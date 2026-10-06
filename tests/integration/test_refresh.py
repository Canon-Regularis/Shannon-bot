"""Mirroring the open backlog nothing ever opened a thread for. Issue #74.

The headline is `test_nobody_is_pinged`. Everything else here is about counting correctly, and a
wrong count is a sentence somebody reads; a refresh that pings is forty people's notifications in
one go, and it only happens once, because the claim on `item_assignments` makes every run after
the first quiet. That is precisely why it cannot be left to be noticed.
"""

from __future__ import annotations

import logging
from dataclasses import replace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.container import _refresh
from shannon.db.models import ItemAssignment, Repository, TrackedItem
from shannon.db.stores.user_links import UserLinkStore
from shannon.discord_bot.errors import DiscordPermissionError
from shannon.domain.enums import ActorRole, ObjectType
from shannon.domain.errors import NotRegisteredError, RepositoryMismatchError
from shannon.domain.models import (
    Actor,
    IssueSnapshot,
    Label,
    PullRequestSnapshot,
    RepositorySnapshot,
)
from shannon.github.errors import (
    GitHubAuthError,
    GitHubNotFoundError,
    GitHubRateLimitError,
)
from shannon.services.boards import BoardNotLinkedError, BoardUnreadableError
from shannon.services.projects import ProjectPoller
from shannon.services.sync.items import SyncOutcome, SyncResult, build_item_sync
from shannon.services.sync.manual import SyncFailedError
from shannon.services.sync.policies import IssuePolicy, PullRequestPolicy, TicketPolicy
from shannon.services.sync.refresh import (
    MissedTickets,
    RefreshScope,
    RepositoryRefresh,
)
from shannon.services.workflow import build_item_workflow
from tests.fakes.board_credentials import FakeBoardCredentials
from tests.fakes.boards import PROJECT, FakeBoard, card, wraps
from tests.fakes.github import FakeGitHubClient
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads
from tests.support.db import link_board, map_channel

pytestmark = pytest.mark.integration

# Where a mirrored card goes. Tickets have no fallback channel, so every ticket test has to
# map one: `TicketPolicy.channel_fallback` is None on purpose, because mirroring a board is a
# thing somebody chooses rather than a thing that happens.
TICKETS = 4242

REPO = RepositorySnapshot(
    github_repo_id=payloads.REPO_ID,
    owner=payloads.OWNER,
    name=payloads.REPO,
    html_url=f"https://github.com/{payloads.OWNER}/{payloads.REPO}",
)
FULL_NAME = REPO.full_name


def a_pull_request(number: int, **overrides) -> PullRequestSnapshot:
    return replace(
        PullRequestSnapshot(
            repository=REPO,
            github_object_id=700_000 + number,
            number=number,
            title=f"Pull request {number}",
            html_url=f"https://github.com/{FULL_NAME}/pull/{number}",
            state="open",
            author=Actor("octocat"),
            reviewers=(Actor("monalisa"),),
            labels=(Label("backend"),),
        ),
        **overrides,
    )


def an_issue(number: int, **overrides) -> IssueSnapshot:
    return replace(
        IssueSnapshot(
            repository=REPO,
            github_object_id=800_000 + number,
            number=number,
            title=f"Issue {number}",
            html_url=f"https://github.com/{FULL_NAME}/issues/{number}",
            state="open",
            author=Actor("octocat"),
            assignees=(Actor("hubot"),),
        ),
        **overrides,
    )


def github_with(*, pulls=(), issues=()) -> FakeGitHubClient:
    key = FULL_NAME.lower()
    return FakeGitHubClient(
        repositories={key: REPO},
        pull_requests={(key, item.number): item for item in pulls},
        issues={(key, item.number): item for item in issues},
    )


def refresh_with(
    sessionmaker: async_sessionmaker[AsyncSession],
    threads: FakeThreadGateway,
    github: FakeGitHubClient,
    *,
    cap: int = 25,
    board: FakeBoard | None = None,
    pull_requests=None,
    issues=None,
    tickets=None,
) -> RepositoryRefresh:
    """The service as the container builds it: all three sync services with no notifier, and
    blocks that name people in plain text.

    An empty `FakeBoard` by default, so a test that says nothing about tickets reads a board with
    no cards on it rather than failing to build. Whether the board is ever ASKED is decided by the
    repository row, not here: with no `project_number` the service never reaches it.
    """
    return RepositoryRefresh(
        sessionmaker,
        github,
        board if board is not None else FakeBoard(),
        pull_requests=pull_requests
        or build_item_sync(sessionmaker, threads, PullRequestPolicy(), mentions=False),
        issues=issues or build_item_sync(sessionmaker, threads, IssuePolicy(), mentions=False),
        tickets=tickets or build_item_sync(sessionmaker, threads, TicketPolicy(), mentions=False),
        cap=cap,
    )


@pytest_asyncio.fixture
async def board_linked(registered: Repository, db_session: AsyncSession) -> None:
    """A server set up for tickets the way somebody who finished the job would have it.

    BOTH commands, because tickets need both and either one missing is its own reported reason:
    `/board link` links the board, `/set_channel project tickets` says where the cards go. A ticket
    has no fallback channel, unlike an issue, which borrows the pull request one until it is given
    its own.
    """
    await link_board(db_session, registered)
    await map_channel(db_session, registered, ObjectType.TICKET, channel_id=TICKETS)


async def link_everybody(session: AsyncSession) -> None:
    """Both people the backlog below names, with Discord accounts against them.

    Without this there is no mention for a block to carry, and an assertion that it carries none
    holds on a server where nobody has ever run `/link`, which is not the claim being made.
    """
    store = UserLinkStore(session)
    await store.link(
        guild_id=1, github_username="monalisa", github_user_id=200, discord_user_id=555
    )
    await store.link(guild_id=1, github_username="hubot", github_user_id=100, discord_user_id=444)
    await session.commit()


def mentions_in_the_blocks(threads: FakeThreadGateway) -> list[str]:
    return [
        threads.metadata_of(thread.thread_id)
        for thread in threads.created
        if "<@" in threads.metadata_of(thread.thread_id)
    ]


class TestMirroringTheBacklog:
    async def test_every_untracked_open_item_gets_a_thread(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        github = github_with(pulls=[a_pull_request(7)], issues=[an_issue(12), an_issue(13)])

        outcome = await refresh_with(db_sessionmaker, threads, github).refresh(
            guild_id=1, scope=RefreshScope.EVERYTHING
        )

        assert (outcome.mirrored, outcome.already, outcome.left) == (3, 0, 0)
        assert len(threads.created) == 3
        assert outcome.full_name == FULL_NAME

    async def test_nobody_is_pinged(
        self,
        registered: Repository,
        db_session: AsyncSession,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """The whole reason this service gets its own sync services rather than the ones `/pr`
        holds. A backlog is not news, and the claim on `item_assignments` means a run that pinged
        would be quiet the second time, so this would look fine in every test after the first.

        The block counts as a ping. Opening a thread posts one, a posted message notifies
        everybody it mentions, and forty of them in one go is exactly what this command must not
        do. Looking only at `threads.posts` missed that for as long as it was the only assertion.
        """
        await link_everybody(db_session)
        github = github_with(pulls=[a_pull_request(7)], issues=[an_issue(12)])

        await refresh_with(db_sessionmaker, threads, github).refresh(
            guild_id=1, scope=RefreshScope.EVERYTHING
        )

        assert threads.posts == [], "a refresh said something in a thread"
        assert mentions_in_the_blocks(threads) == [], "a refresh notified people through a block"
        stamps = await db_session.scalars(select(ItemAssignment.notified_at))
        assert list(stamps) != [], "no assignment rows, so this proves nothing"
        assert all(stamp is None for stamp in stamps), "a refresh spent somebody's one ping"

    async def test_the_wiring_hands_it_services_that_cannot_ping(
        self,
        registered: Repository,
        db_session: AsyncSession,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """The service the container builds, rather than one this file assembled.

        The test above proves the service is silent when it is handed silent sync services. This
        proves it is handed them, which is the half that lives in the wiring and the half a later
        edit can undo without any of the rest of this file noticing.

        All THREE kinds, including the ticket sync the container grew for this. The poller builds
        its own ticket sync with mentions left ON - harmless there, because it mirrors one card at
        a time - so the one here is the only place the difference is recorded, and copying the
        poller's line would have been the natural mistake.
        """
        await link_everybody(db_session)
        await link_board(db_session, registered)
        await map_channel(db_session, registered, ObjectType.TICKET, channel_id=TICKETS)
        github = github_with(pulls=[a_pull_request(7)], issues=[an_issue(12)])

        await _refresh(db_sessionmaker, github, threads, FakeBoard(card())).refresh(
            guild_id=1, scope=RefreshScope.EVERYTHING
        )

        assert len(threads.created) == 3, "it mirrored nothing, so this proves nothing"
        assert {thread.channel_id for thread in threads.created} == {99, 98, TICKETS}
        assert threads.posts == [], "the wiring gave /refresh a sync service that pings"
        assert mentions_in_the_blocks(threads) == [], "the wiring gave it one that mentions"
        stamps = await db_session.scalars(select(ItemAssignment.notified_at))
        assert all(stamp is None for stamp in stamps)

    async def test_an_item_that_already_has_a_thread_is_left_alone(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        github = github_with(issues=[an_issue(12)])
        service = refresh_with(db_sessionmaker, threads, github)
        await service.refresh(guild_id=1, scope=RefreshScope.ISSUES)
        written = list(threads.updates)

        outcome = await service.refresh(guild_id=1, scope=RefreshScope.ISSUES)

        assert (outcome.mirrored, outcome.already, outcome.left) == (0, 1, 0)
        assert len(threads.created) == 1
        assert threads.updates == written, "it rewrote a thread it was told to leave alone"

    async def test_a_row_with_no_thread_counts_as_untracked(
        self,
        registered: Repository,
        db_session: AsyncSession,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """The row is committed before the Discord call that gives it a thread, so a refused
        create leaves an item recorded here and invisible in the channel. Reading the row alone
        would call that tracked and leave it that way for ever.
        """
        orphan = an_issue(12)
        db_session.add(
            TrackedItem(
                repository_id=registered.id,
                github_object_id=orphan.github_object_id,
                github_object_type=ObjectType.ISSUE,
                github_object_number=orphan.number,
                github_url=orphan.html_url,
                title=orphan.title,
                github_state="open",
            )
        )
        await db_session.commit()

        outcome = await refresh_with(
            db_sessionmaker, threads, github_with(issues=[orphan])
        ).refresh(guild_id=1, scope=RefreshScope.ISSUES)

        assert (outcome.mirrored, outcome.already) == (1, 0)
        assert len(threads.created) == 1

    async def test_a_closed_item_is_never_reached_for(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        github = github_with(issues=[an_issue(12, state="closed")])

        outcome = await refresh_with(db_sessionmaker, threads, github).refresh(
            guild_id=1, scope=RefreshScope.ISSUES
        )

        assert (outcome.mirrored, outcome.already, outcome.left) == (0, 0, 0)
        assert threads.created == []

    async def test_a_repository_with_nothing_open_is_not_a_failure(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        outcome = await refresh_with(db_sessionmaker, threads, github_with()).refresh(
            guild_id=1, scope=RefreshScope.EVERYTHING
        )

        assert (outcome.mirrored, outcome.already, outcome.left) == (0, 0, 0)


class TestWhatEachScopeReads:
    async def test_issues_only_never_asks_for_pull_requests(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        github = github_with(pulls=[a_pull_request(7)], issues=[an_issue(12)])

        outcome = await refresh_with(db_sessionmaker, threads, github).refresh(
            guild_id=1, scope=RefreshScope.ISSUES
        )

        assert [kind for kind, _ in github.list_calls] == ["issues"]
        assert outcome.mirrored == 1

    async def test_pull_requests_only_never_asks_for_issues(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        github = github_with(pulls=[a_pull_request(7)], issues=[an_issue(12)])

        outcome = await refresh_with(db_sessionmaker, threads, github).refresh(
            guild_id=1, scope=RefreshScope.PULL_REQUESTS
        )

        assert [kind for kind, _ in github.list_calls] == ["pulls"]
        assert outcome.mirrored == 1

    async def test_tickets_only_never_asks_github_for_items(
        self,
        board_linked: None,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """The board is a different reader on a different token, so a ticket run must not spend a
        single call on the repository listings."""
        github = github_with(pulls=[a_pull_request(7)], issues=[an_issue(12)])
        board = FakeBoard(card())

        outcome = await refresh_with(db_sessionmaker, threads, github, board=board).refresh(
            guild_id=1, scope=RefreshScope.TICKETS
        )

        assert github.list_calls == [], "it asked GitHub for items on a tickets-only run"
        assert board.reads == [(payloads.OWNER, PROJECT)]
        assert outcome.mirrored == 1

    async def test_everything_reads_all_three(
        self,
        board_linked: None,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """And the board LAST, which is the order the cap is spent in: a capped run should go on
        reviews before it goes on cards."""
        github = github_with(pulls=[a_pull_request(7)], issues=[an_issue(12)])
        board = FakeBoard(card())

        outcome = await refresh_with(db_sessionmaker, threads, github, board=board).refresh(
            guild_id=1, scope=RefreshScope.EVERYTHING
        )

        assert [kind for kind, _ in github.list_calls] == ["pulls", "issues"]
        assert board.reads == [(payloads.OWNER, PROJECT)], "the board was not read at all"
        assert outcome.mirrored == 3
        assert outcome.tickets_missed is None

    async def test_everything_with_no_board_still_reads_both_lists(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """A server with no board is the common case for a bot in several, and `all` must keep
        working for it. The run says so rather than quietly reporting a short count."""
        github = github_with(pulls=[a_pull_request(7)], issues=[an_issue(12)])
        board = FakeBoard(card())

        outcome = await refresh_with(db_sessionmaker, threads, github, board=board).refresh(
            guild_id=1, scope=RefreshScope.EVERYTHING
        )

        assert [kind for kind, _ in github.list_calls] == ["pulls", "issues"]
        assert board.reads == [], "it read a board this server never linked"
        assert outcome.mirrored == 2, "the pull request and the issue were lost with the tickets"
        assert outcome.tickets_missed is MissedTickets.NO_BOARD


class TestWhatTicketsAreCovered:
    """Draft cards only. A card wrapping an issue or a pull request already has a thread from that
    item's own webhooks, so the other two scopes cover it and this one must leave it alone.
    """

    async def test_a_draft_card_with_no_thread_gets_one(
        self,
        board_linked: None,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        outcome = await refresh_with(
            db_sessionmaker, threads, github_with(), board=FakeBoard(card())
        ).refresh(guild_id=1, scope=RefreshScope.TICKETS)

        assert outcome.mirrored == 1
        assert threads.created[0].channel_id == TICKETS

    async def test_a_card_that_already_has_a_thread_is_left_alone(
        self,
        board_linked: None,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """The second run is the test. A card is keyed by its CARD id, not by any GitHub object,
        so this also pins that the row written by the first run is the row the second one finds."""
        service = refresh_with(db_sessionmaker, threads, github_with(), board=FakeBoard(card()))

        first = await service.refresh(guild_id=1, scope=RefreshScope.TICKETS)
        second = await service.refresh(guild_id=1, scope=RefreshScope.TICKETS)

        assert (first.mirrored, first.already) == (1, 0)
        assert (second.mirrored, second.already, second.left) == (0, 1, 0)
        assert len(threads.created) == 1, "it opened a second thread for the same card"

    async def test_a_card_wrapping_an_issue_is_never_mirrored_as_a_ticket(
        self,
        board_linked: None,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """The other arm of the draft filter, and the user decision this scope was built on. A
        wrapped card has a thread of its own already; mirroring it here would open a second one
        under a title the board chose, and the row would collide with nothing it could be found by.
        """
        board = FakeBoard(card(), wraps(ObjectType.ISSUE, content_id=999))

        outcome = await refresh_with(db_sessionmaker, threads, github_with(), board=board).refresh(
            guild_id=1, scope=RefreshScope.TICKETS
        )

        assert outcome.mirrored == 1, "the wrapped card was mirrored as a ticket"
        assert len(threads.created) == 1

    async def test_a_board_listing_a_card_twice_mirrors_it_once(
        self,
        board_linked: None,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """A board is paged by cursor and a cursor is not a snapshot, so a card can arrive twice.
        The poller's tests cover the dedupe itself; this proves the refresh path goes through it.
        """
        board = FakeBoard(card(), card())

        outcome = await refresh_with(db_sessionmaker, threads, github_with(), board=board).refresh(
            guild_id=1, scope=RefreshScope.TICKETS
        )

        assert (outcome.mirrored, outcome.already, outcome.left) == (1, 0, 0)

    async def test_nobody_is_pinged_for_a_card_either(
        self,
        board_linked: None,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """A guard rather than a find: a draft card names nobody today, so there is nothing for the
        ticket sync to suppress. It is here so that a card which one day carries an assignee cannot
        start notifying a backlog without this file noticing."""
        await refresh_with(
            db_sessionmaker, threads, github_with(), board=FakeBoard(card())
        ).refresh(guild_id=1, scope=RefreshScope.TICKETS)

        assert len(threads.created) == 1, "it mirrored nothing, so this proves nothing"
        assert threads.posts == []
        assert mentions_in_the_blocks(threads) == []


class TestWhenTicketsCannotBeCovered:
    """Three reasons, and each one is a note under `all` and a refusal under `tickets`.

    The split is the whole design. Under `all` the pull requests and issues were real work, and
    telling somebody nothing happened would be a lie; under `tickets` they asked for exactly the
    thing that cannot happen, and a green panel reporting zero would be worse than a sentence
    naming the command that puts it right.
    """

    async def test_asking_for_tickets_with_no_board_is_refused(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        with pytest.raises(BoardNotLinkedError, match="/board link"):
            await refresh_with(db_sessionmaker, threads, github_with()).refresh(
                guild_id=1, scope=RefreshScope.TICKETS
            )

    async def test_no_ticket_channel_is_a_note_under_all(
        self,
        registered: Repository,
        db_session: AsyncSession,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """The case that made this a pre-check. Without one, `all` mirrored every pull request and
        issue, reached the first card, and raised - so the reply said nothing had been done while
        the threads were already there. A board linked with no channel mapped is an ordinary
        half-finished setup, because tickets need both commands.
        """
        await link_board(db_session, registered)
        github = github_with(pulls=[a_pull_request(7)], issues=[an_issue(12)])
        board = FakeBoard(card())

        outcome = await refresh_with(db_sessionmaker, threads, github, board=board).refresh(
            guild_id=1, scope=RefreshScope.EVERYTHING
        )

        assert outcome.mirrored == 2, "the work that could be done was thrown away"
        assert outcome.tickets_missed is MissedTickets.NO_CHANNEL
        assert board.reads == [], "it read the board with nowhere to put what came back"

    async def test_asking_for_tickets_with_no_channel_is_refused(
        self,
        registered: Repository,
        db_session: AsyncSession,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """What this pins is the SENTENCE, not the pre-check. Measured: with the pre-check removed
        this still passes, because the board is then read, the first card answers NOT_TRACKED, and
        `_mirror` raises with the same `/set_channel` words. The pre-check is pinned by
        `test_no_ticket_channel_is_a_note_under_all`, which does go red without it.
        """
        await link_board(db_session, registered)

        with pytest.raises(SyncFailedError, match="/set_channel"):
            await refresh_with(
                db_sessionmaker, threads, github_with(), board=FakeBoard(card())
            ).refresh(guild_id=1, scope=RefreshScope.TICKETS)

    async def test_a_board_that_will_not_read_is_a_note_under_all(
        self,
        board_linked: None,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """A board's authorisation is its linker's, and once it lapses or is withdrawn it stays
        that way until somebody authorises again - so refusing would break `all` for this server
        until somebody noticed. The poller makes the same judgement about the same read."""
        github = github_with(pulls=[a_pull_request(7)], issues=[an_issue(12)])
        board = FakeBoard(card())
        board.error = GitHubAuthError("nobody's authorisation stands behind this board")

        outcome = await refresh_with(db_sessionmaker, threads, github, board=board).refresh(
            guild_id=1, scope=RefreshScope.EVERYTHING
        )

        assert outcome.mirrored == 2, "a token problem took the pull requests down with it"
        assert outcome.tickets_missed is MissedTickets.UNREADABLE

    async def test_a_spent_project_quota_is_the_same_answer(
        self,
        board_linked: None,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """Folded in with the other two rather than given a fourth sentence. An operator cannot act
        on the difference from a Discord reply, and the log carries which it was.

        Note this is the one place `/refresh` swallows a rate limit rather than handing it back: the
        REPOSITORY listings still propagate one, because those are the same quota the whole bot
        runs on, and the board is a separate token.
        """
        board = FakeBoard(card())
        board.error = GitHubRateLimitError("the project quota is spent")

        outcome = await refresh_with(
            db_sessionmaker, threads, github_with(issues=[an_issue(12)]), board=board
        ).refresh(guild_id=1, scope=RefreshScope.EVERYTHING)

        assert outcome.mirrored == 1
        assert outcome.tickets_missed is MissedTickets.UNREADABLE

    async def test_asking_for_tickets_on_a_board_that_will_not_read_is_refused(
        self,
        board_linked: None,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        board = FakeBoard(card())
        board.error = GitHubNotFoundError("no such board")

        with pytest.raises(BoardUnreadableError):
            await refresh_with(db_sessionmaker, threads, github_with(), board=board).refresh(
                guild_id=1, scope=RefreshScope.TICKETS
            )


class TestACardWhoseMirrorFailed:
    """The state a half-finished mirror leaves, and why a ticket needs undoing where an issue does
    not.

    An issue or a pull request that fails here is found again on its own: the row is written before
    the Discord call, so a refused thread leaves `discord_thread_id` null and `_threaded` keys on
    exactly that. A card can land somewhere neither guard sees. `ItemThreads` attaches the thread to
    the row BEFORE it re-raises, which is what happens when Discord opens the thread and then
    refuses the message in it - so the row ends up holding the card's timestamp AND a thread id,
    `_threaded` counts it as done, and the poller's `_has_moved` sees a timestamp that has not
    moved. Nothing anywhere comes back for it.
    """

    async def test_the_row_is_put_back_so_something_comes_looking_again(
        self,
        board_linked: None,
        db_session: AsyncSession,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        threads.fail_next_first_message = True

        outcome = await refresh_with(
            db_sessionmaker, threads, github_with(), board=FakeBoard(card())
        ).refresh(guild_id=1, scope=RefreshScope.TICKETS)

        assert (outcome.mirrored, outcome.failed) == (0, 1)
        db_session.expire_all()
        row = await db_session.scalar(
            select(TrackedItem).where(TrackedItem.github_object_type == ObjectType.TICKET)
        )
        assert row is not None, "no row at all, so this proves nothing"
        assert row.discord_thread_id is not None, (
            "the thread was never attached, so the stranding this undoes did not happen and the "
            "assertion below would hold for the wrong reason"
        )
        assert row.github_updated_at is None, (
            "the card is still recorded as current, so nothing will ever look at it again"
        )

    async def test_a_later_run_of_the_poller_picks_the_card_up(
        self,
        board_linked: None,
        registered: Repository,
        db_session: AsyncSession,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """The claim the test above only pins the mechanism of, and it is CROSS-SERVICE on purpose.

        Note what is NOT true: a second `/refresh` does not retry it. The row still carries a thread
        id, so `_threaded` keeps counting it as done. What the compensation restores is the poller's
        ability to revisit, which is why `to=None` rather than the value that was there - the value
        that was there is one `_has_moved` would skip.
        """
        threads.fail_next_first_message = True
        board = FakeBoard(card())
        await refresh_with(db_sessionmaker, threads, github_with(), board=board).refresh(
            guild_id=1, scope=RefreshScope.TICKETS
        )
        opened = len(threads.created)

        poller = ProjectPoller(
            db_sessionmaker,
            board,
            build_item_sync(db_sessionmaker, threads, TicketPolicy()),
            build_item_workflow(
                db_sessionmaker,
                github_with(),
                threads,
                pr_sync=build_item_sync(db_sessionmaker, threads, PullRequestPolicy()),
                issue_sync=build_item_sync(db_sessionmaker, threads, IssuePolicy()),
                authorisations=FakeBoardCredentials(),
            ),
            threads,
            polling=True,
        )
        mirrored = await poller.run_once()

        assert opened == 1, "the refresh never attached a thread, so this proves nothing"
        assert mirrored == 1, "the poller passed over a card stranded behind an empty thread"

    async def test_a_failing_pull_request_never_touches_a_cards_row(
        self,
        board_linked: None,
        db_session: AsyncSession,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """The other arm of the guard, and it is load-bearing rather than tidy.

        `forget_the_mirror` writes with `object_type=TICKET` hardcoded, because that is the only
        kind it is for. A card is keyed in `github_object_id` by its CARD id while a pull request is
        keyed there by its GITHUB id, so the same number can name both - and compensating a pull
        request would issue that write against whatever CARD happens to carry its number, wiping a
        healthy ticket's timestamp and making the poller mirror it all over again.

        Two drafts of this test were wrong before this one. Asserting that a pull request keeps its
        `github_updated_at` fails, because a refused first message leaves that null for every kind;
        asserting that a later run retries it fails too, because `ItemThreads` attaches the thread
        before re-raising, so `_threaded` counts it done whatever kind it is. The difference the
        guard makes is not to the pull request at all - it is to the card next to it.
        """
        collides = 700_007
        board = FakeBoard(card(item_id=collides))
        service = refresh_with(
            db_sessionmaker,
            threads,
            github_with(pulls=[a_pull_request(7, github_object_id=collides)]),
            board=board,
        )

        await service.refresh(guild_id=1, scope=RefreshScope.TICKETS)
        db_session.expire_all()
        healthy = await db_session.scalar(
            select(TrackedItem.github_updated_at).where(
                TrackedItem.github_object_type == ObjectType.TICKET
            )
        )
        assert healthy is not None, "the card was never mirrored, so this proves nothing"

        threads.fail_next_first_message = True
        outcome = await service.refresh(guild_id=1, scope=RefreshScope.PULL_REQUESTS)

        assert outcome.failed == 1, "the pull request did not fail, so this proves nothing"
        db_session.expire_all()
        after = await db_session.scalar(
            select(TrackedItem.github_updated_at).where(
                TrackedItem.github_object_type == ObjectType.TICKET
            )
        )
        assert after == healthy, (
            "a failing pull request put a CARD back, because they share a column and the "
            "compensation only ever means anything for one of them"
        )


class TestTheCap:
    async def test_it_mirrors_no_more_than_the_cap_and_says_what_is_left(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        github = github_with(issues=[an_issue(12), an_issue(13), an_issue(14)])

        outcome = await refresh_with(db_sessionmaker, threads, github, cap=2).refresh(
            guild_id=1, scope=RefreshScope.ISSUES
        )

        assert (outcome.mirrored, outcome.left) == (2, 1)
        assert len(threads.created) == 2

    async def test_a_second_run_carries_on_where_the_first_stopped(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        github = github_with(issues=[an_issue(12), an_issue(13), an_issue(14)])
        service = refresh_with(db_sessionmaker, threads, github, cap=2)
        await service.refresh(guild_id=1, scope=RefreshScope.ISSUES)

        outcome = await service.refresh(guild_id=1, scope=RefreshScope.ISSUES)

        assert (outcome.mirrored, outcome.already, outcome.left) == (1, 2, 0)
        assert len(threads.created) == 3

    async def test_the_count_left_covers_every_kind(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """Every list is read before any of them is mirrored, even when the first exhausts the cap.
        Otherwise "how many are still untracked" is answerable only for the kind it got to.
        """
        github = github_with(pulls=[a_pull_request(7), a_pull_request(8)], issues=[an_issue(12)])

        outcome = await refresh_with(db_sessionmaker, threads, github, cap=1).refresh(
            guild_id=1, scope=RefreshScope.EVERYTHING
        )

        assert (outcome.mirrored, outcome.left) == (1, 2)
        assert [kind for kind, _ in github.list_calls] == ["pulls", "issues"]

    async def test_tickets_share_the_one_cap_and_are_counted_in_what_is_left(
        self,
        board_linked: None,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """One cap across all three kinds, not one each, and that is about the interaction token.

        A command has fifteen minutes after it defers. The worst case is the cap times the lock
        wait, plus two Discord calls per item, and twenty-five was chosen against that. Giving
        tickets a cap of their own would double the worst case rather than add a feature, so this
        pins the shared cap: with the cap spent on pull requests, no card is opened, and the cards
        are counted in what is left so the reply tells somebody to run it again.
        """
        github = github_with(pulls=[a_pull_request(7), a_pull_request(8)])
        board = FakeBoard(card(item_id=901), card(item_id=902))

        outcome = await refresh_with(db_sessionmaker, threads, github, board=board, cap=2).refresh(
            guild_id=1, scope=RefreshScope.EVERYTHING
        )

        assert outcome.mirrored == 2
        assert outcome.left == 2, "the cards were not counted in what is still untracked"
        assert board.reads != [], "the board was never read, so this proves nothing"
        assert {thread.channel_id for thread in threads.created} == {99}, (
            "a card was opened with the cap already spent on reviews"
        )


class TestWhenOneItemFails:
    async def test_the_rest_are_still_mirrored(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        github = github_with(issues=[an_issue(12), an_issue(13)])
        threads.fail_next_create = True

        with caplog.at_level(logging.WARNING):
            outcome = await refresh_with(db_sessionmaker, threads, github).refresh(
                guild_id=1, scope=RefreshScope.ISSUES
            )

        assert (outcome.mirrored, outcome.failed) == (1, 1)
        assert outcome.left == 1, "the one that failed is still untracked"
        assert "could not mirror" in caplog.text

    async def test_a_surprise_does_not_strand_the_command(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Deliberately unlike `/pr`, which lets an unexpected failure out. There it is one item
        and the person sees the tree's answer; here it would abandon every item after it and
        leave the command with no reply at all.
        """

        class _Surprising(FakeThreadGateway):
            """Fails the first item and nothing else. Counted rather than read off `created`,
            which stays empty precisely because the first one failed."""

            attempts = 0

            async def create(self, **kwargs):
                self.attempts += 1
                if self.attempts == 1:
                    raise RuntimeError("something nobody predicted")
                return await super().create(**kwargs)

        broken = _Surprising()
        github = github_with(issues=[an_issue(12), an_issue(13)])

        with caplog.at_level(logging.ERROR):
            outcome = await refresh_with(db_sessionmaker, broken, github).refresh(
                guild_id=1, scope=RefreshScope.ISSUES
            )

        assert (outcome.mirrored, outcome.failed) == (1, 1)
        assert "an unexpected failure" in caplog.text

    async def test_an_item_a_newer_sync_overtook_is_neither_mirrored_nor_failed(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """A webhook can attach a thread between the read and the sync. Nothing went wrong and
        nothing was opened, so it belongs in `left`; the next run finds a thread and skips it.
        """

        class _Overtaken:
            async def sync(self, snapshot, **_):
                return SyncResult(outcome=SyncOutcome.STALE, tracked_item_id=1, thread_id=2)

        github = github_with(issues=[an_issue(12)])
        service = refresh_with(db_sessionmaker, threads, github, issues=_Overtaken())

        outcome = await service.refresh(guild_id=1, scope=RefreshScope.ISSUES)

        assert (outcome.mirrored, outcome.failed, outcome.left) == (0, 0, 1)


class TestWhatStopsTheRun:
    async def test_no_channel_mapped_stops_at_the_first_item(
        self,
        db_session: AsyncSession,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """Nothing about the item decided that, so every one after it would be refused the same
        way, each opening a session and writing the same warning.

        Issues fall back to the pull request channel, so this is a repository with no mapping at
        all rather than one missing its issue channel, which would have worked.
        """
        db_session.add(
            Repository(
                github_repo_id=REPO.github_repo_id,
                repo_name=FULL_NAME,
                repo_url=REPO.html_url,
                discord_guild_id=1,
            )
        )
        await db_session.commit()
        github = github_with(issues=[an_issue(12), an_issue(13)])

        with pytest.raises(SyncFailedError, match="/set_channel"):
            await refresh_with(db_sessionmaker, threads, github).refresh(
                guild_id=1, scope=RefreshScope.ISSUES
            )

        assert threads.created == []

    async def test_an_unregistered_server_is_told_to_register(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], threads: FakeThreadGateway
    ) -> None:
        with pytest.raises(NotRegisteredError, match="/register"):
            await refresh_with(db_sessionmaker, threads, github_with()).refresh(
                guild_id=1, scope=RefreshScope.EVERYTHING
            )

    async def test_a_name_that_now_serves_somebody_elses_repository_is_refused(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """The hazard `/pr` already guards: a freed name taken by somebody else would have this
        mirror a stranger's whole backlog into the channel."""
        stranger = replace(REPO, github_repo_id=REPO.github_repo_id + 1)
        github = github_with(issues=[an_issue(12)])
        github.repositories[FULL_NAME.lower()] = stranger

        with pytest.raises(RepositoryMismatchError, match="Somebody else has taken it"):
            await refresh_with(db_sessionmaker, threads, github).refresh(
                guild_id=1, scope=RefreshScope.EVERYTHING
            )

        assert threads.created == []

    async def test_a_spent_rate_limit_comes_back_untouched(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """Waiting is the only thing that helps, and the reply table already says how long."""
        github = github_with(issues=[an_issue(12)])
        github.error = GitHubRateLimitError("spent", retry_after=600)

        with pytest.raises(GitHubRateLimitError):
            await refresh_with(db_sessionmaker, threads, github).refresh(
                guild_id=1, scope=RefreshScope.EVERYTHING
            )

    async def test_a_refused_thread_is_one_item_rather_than_the_run(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A permission is permanent, which everywhere else in this project means the delivery
        is dropped. Here it is still one item: the next may live in a channel this bot can write
        in, and abandoning the run would leave the command with no reply.
        """

        class _Refuses(FakeThreadGateway):
            async def create(self, **kwargs):
                raise DiscordPermissionError("Discord will not let the bot create a thread")

        refusing = _Refuses()
        github = github_with(issues=[an_issue(12)])

        with caplog.at_level(logging.WARNING):
            outcome = await refresh_with(db_sessionmaker, refusing, github).refresh(
                guild_id=1, scope=RefreshScope.ISSUES
            )

        assert (outcome.mirrored, outcome.failed) == (0, 1)
        assert "could not mirror" in caplog.text


class TestTheRoleRowsItWrites:
    async def test_the_people_on_an_item_are_still_recorded(
        self,
        registered: Repository,
        db_session: AsyncSession,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """Silent is not the same as incomplete. The block names everybody, the rows are written,
        and the only thing withheld is the notification.
        """
        github = github_with(pulls=[a_pull_request(7)])

        await refresh_with(db_sessionmaker, threads, github).refresh(
            guild_id=1, scope=RefreshScope.PULL_REQUESTS
        )

        roles = await db_session.scalars(
            select(ItemAssignment.role_type).where(ItemAssignment.github_username == "monalisa")
        )
        assert ActorRole.REVIEWER in set(roles)
