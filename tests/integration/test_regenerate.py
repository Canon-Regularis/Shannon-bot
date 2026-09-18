"""Redrawing an item's block from GitHub, including one whose thread is closed and locked.

Issue #65. Two things are being proved here and they are easy to conflate.

The first is that a redraw reaches a thread nothing else can: closed, archived and locked, with no
further delivery coming. That path already worked and is pinned so it keeps working.

The second is the bug. A thread opened by `/refresh` renders everybody in plain text, and the only
thing that ever converts them is a later sync, which for a quiet or closed item never happens. So
the test that matters most below opens a thread exactly the way `/refresh` does, links somebody
afterwards, and asserts the redraw puts the mention in without ringing anybody.
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.container import _regenerate
from shannon.db.models import ChannelMapping, Repository, TrackedItem
from shannon.db.stores.user_links import UserLinkStore
from shannon.domain.enums import ObjectType
from shannon.domain.errors import RepositoryMismatchError
from shannon.github.errors import GitHubNotFoundError
from shannon.services.sync.items import build_item_sync
from shannon.services.sync.manual import SyncFailedError
from shannon.services.sync.policies import IssuePolicy, PullRequestPolicy
from shannon.services.sync.regenerate import ItemRegeneration
from shannon.services.workflow import (
    ItemKind,
    NotAnItemThreadError,
    WorkflowRefusedError,
)
from tests.fakes.github import FakeGitHubClient
from tests.fakes.threads import FakeThreadGateway

pytestmark = pytest.mark.integration

ALICE = 555


async def link(session: AsyncSession, login: str, account: int, discord_id: int) -> None:
    await UserLinkStore(session).link(
        guild_id=1, github_username=login, github_user_id=account, discord_user_id=discord_id
    )
    await session.commit()


def redrawing(
    sessionmaker: async_sessionmaker,
    threads: FakeThreadGateway,
    *,
    pull_request=None,
    issue=None,
) -> ItemRegeneration:
    """The service the container builds: no notifier, and no allow-list.

    `notifies=False` is the whole of what separates this from `/pr`. The block still names people
    as live mentions, which is the bug fix, and Discord is told to ring none of them.
    """
    kinds = {}
    if pull_request is not None:
        kinds[ObjectType.PR] = ItemKind(
            fetch=pull_request,
            sync=build_item_sync(sessionmaker, threads, PullRequestPolicy(), notifies=False),
        )
    if issue is not None:
        kinds[ObjectType.ISSUE] = ItemKind(
            fetch=issue,
            sync=build_item_sync(sessionmaker, threads, IssuePolicy(), notifies=False),
        )
    return ItemRegeneration(sessionmaker, kinds)


def answering(snapshot):
    async def fetch(owner: str, name: str, number: int):
        return snapshot

    return fetch


def refusing(error: Exception):
    async def fetch(owner: str, name: str, number: int):
        raise error

    return fetch


def block_of(threads: FakeThreadGateway, thread_id: int) -> str:
    return threads.metadata_of(thread_id)


def last_write(threads: FakeThreadGateway) -> tuple[str, int, str, tuple | None]:
    return threads.allowed[-1]


class TestTheBugThisExistsFor:
    async def test_somebody_linked_after_the_thread_opened_becomes_a_mention(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker,
        db_session: AsyncSession,
        pr_event,
    ) -> None:
        """The whole issue in one test.

        The thread is opened the way `/refresh` opens one, with mentions off, so the block names
        `monalisa` in plain text. Then she links. Nothing in the project re-reads that mapping for
        this item ever again: `/refresh` skips anything already threaded, `/link` writes a row and
        touches no thread, and a quiet item sends no webhook.
        """
        threads = FakeThreadGateway()
        opened = pr_event("opened")
        await build_item_sync(db_sessionmaker, threads, PullRequestPolicy(), mentions=False).sync(
            opened
        )
        thread_id = threads.created[0].thread_id
        assert f"<@{ALICE}>" not in block_of(threads, thread_id), "arranged wrong"

        await link(db_session, "monalisa", 200, ALICE)
        await redrawing(db_sessionmaker, threads, pull_request=answering(opened)).regenerate(
            thread_id=thread_id
        )

        assert f"<@{ALICE}>" in block_of(threads, thread_id)

    async def test_and_rings_nobody_doing_it(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker,
        db_session: AsyncSession,
        pr_event,
    ) -> None:
        """Named and not rung, which is the pair `/mentions off` already relies on. An empty
        allow-list rather than None: None would leave the client's own rule in force."""
        threads = FakeThreadGateway()
        opened = pr_event("opened")
        await build_item_sync(db_sessionmaker, threads, PullRequestPolicy(), mentions=False).sync(
            opened
        )
        await link(db_session, "monalisa", 200, ALICE)

        await redrawing(db_sessionmaker, threads, pull_request=answering(opened)).regenerate(
            thread_id=threads.created[0].thread_id
        )

        _, _, content, notify = last_write(threads)
        assert f"<@{ALICE}>" in content
        assert notify == ()

    async def test_it_posts_no_ping_line(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker,
        db_session: AsyncSession,
        pr_event,
    ) -> None:
        """Built with no notifier rather than told not to ping, so there is nothing to fire and
        no later edit to the sync path can make one fire."""
        threads = FakeThreadGateway()
        opened = pr_event("opened")
        await build_item_sync(db_sessionmaker, threads, PullRequestPolicy()).sync(opened)
        await link(db_session, "monalisa", 200, ALICE)
        before = len(threads.posts)

        await redrawing(db_sessionmaker, threads, pull_request=answering(opened)).regenerate(
            thread_id=threads.created[0].thread_id
        )

        assert len(threads.posts) == before


class TestTheWiringTheContainerActuallyBuilds:
    """The tests above build the service the way the container does. This one builds it THROUGH
    the container, so the two cannot drift apart without something going red.

    Without it, `_regenerate` could be changed to `mentions=False` - which is the natural typo,
    since the neighbouring `_refresh` says exactly that - and every test above would go on passing
    while the feature quietly stopped working.
    """

    async def test_the_wired_service_names_people_and_rings_nobody(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker,
        db_session: AsyncSession,
        pr_event,
    ) -> None:
        threads = FakeThreadGateway()
        opened = pr_event("opened")
        await build_item_sync(db_sessionmaker, threads, PullRequestPolicy(), mentions=False).sync(
            opened
        )
        thread_id = threads.created[0].thread_id
        await link(db_session, "monalisa", 200, ALICE)
        github = FakeGitHubClient(
            pull_requests={(opened.repository.full_name.lower(), opened.number): opened}
        )

        await _regenerate(db_sessionmaker, github, threads).regenerate(thread_id=thread_id)

        _, _, content, notify = last_write(threads)
        assert f"<@{ALICE}>" in content, "the container wired mentions off; that is the old bug"
        assert notify == (), "the container wired the allow-list on"


class TestWhatItAsksGitHubFor:
    async def test_it_asks_about_the_item_the_row_names(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker,
        db_session: AsyncSession,
        pr_event,
    ) -> None:
        """The owner, the name and the number all come off the stored row, because the command
        was given nothing but a thread. Asking about the wrong one would redraw this thread from
        somebody else's item, which is the failure the repository-id check catches only when the
        repository differs too."""
        threads = FakeThreadGateway()
        opened = pr_event("opened")
        await build_item_sync(db_sessionmaker, threads, PullRequestPolicy()).sync(opened)
        asked: list[tuple[str, str, int]] = []

        async def fetch(owner: str, name: str, number: int):
            asked.append((owner, name, number))
            return opened

        await redrawing(db_sessionmaker, threads, pull_request=fetch).regenerate(
            thread_id=threads.created[0].thread_id
        )

        owner, _, name = opened.repository.full_name.partition("/")
        assert asked == [(owner, name, opened.number)]

    async def test_a_repository_renamed_under_the_same_id_is_reported_by_its_new_name(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker,
        db_session: AsyncSession,
        pr_event,
    ) -> None:
        """A rename keeps the id, so it passes the mismatch check and is the ordinary case rather
        than a refusal. The sync follows it, which makes the stored name the command started from
        the stale one, so the reply has to come off the snapshot."""
        from dataclasses import replace

        threads = FakeThreadGateway()
        opened = pr_event("opened")
        await build_item_sync(db_sessionmaker, threads, PullRequestPolicy()).sync(opened)
        renamed = replace(
            opened,
            repository=replace(opened.repository, name="widget-renamed"),
        )

        outcome = await redrawing(
            db_sessionmaker, threads, pull_request=answering(renamed)
        ).regenerate(thread_id=threads.created[0].thread_id)

        assert outcome.full_name.endswith("/widget-renamed")


class TestAThreadNothingElseWillReach:
    async def test_a_closed_pull_request_is_redrawn_and_shut_again(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker,
        db_session: AsyncSession,
        pr_event,
    ) -> None:
        """The audience for this command. The thread is archived and locked, so the write wakes
        it and the sync puts the lock back afterwards."""
        threads = FakeThreadGateway()
        closed = pr_event("closed", state="closed")
        await build_item_sync(db_sessionmaker, threads, PullRequestPolicy()).sync(closed)
        thread_id = threads.created[0].thread_id
        assert threads.threads[thread_id].locked is True, "arranged wrong"

        outcome = await redrawing(
            db_sessionmaker, threads, pull_request=answering(closed)
        ).regenerate(thread_id=thread_id)

        assert outcome.created is False
        assert thread_id in threads.updates
        assert threads.threads[thread_id].locked is True
        assert threads.threads[thread_id].archived is True

    async def test_the_row_still_says_the_thread_is_shut(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker,
        db_session: AsyncSession,
        pr_event,
    ) -> None:
        """`claim_thread` clears the lock only when the pointer moves to a DIFFERENT thread, and
        a redraw swaps a thread for itself. A cleared row would report a finished item's thread as
        one nobody had shut."""
        threads = FakeThreadGateway()
        closed = pr_event("closed", state="closed")
        await build_item_sync(db_sessionmaker, threads, PullRequestPolicy()).sync(closed)

        await redrawing(db_sessionmaker, threads, pull_request=answering(closed)).regenerate(
            thread_id=threads.created[0].thread_id
        )

        db_session.expire_all()
        item = await db_session.scalar(select(TrackedItem))
        assert item is not None
        assert item.discord_thread_locked is True

    async def test_a_closed_issue_too(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker,
        db_session: AsyncSession,
        issue_event,
    ) -> None:
        closed = issue_event("closed", state="closed")
        threads = FakeThreadGateway()
        await build_item_sync(db_sessionmaker, threads, IssuePolicy()).sync(closed)
        thread_id = threads.created[0].thread_id

        outcome = await redrawing(db_sessionmaker, threads, issue=answering(closed)).regenerate(
            thread_id=thread_id
        )

        assert outcome.number == closed.number
        assert threads.threads[thread_id].locked is True

    async def test_a_refused_reshut_is_reported_rather_than_swallowed(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker,
        db_session: AsyncSession,
        pr_event,
    ) -> None:
        """This matters more here than anywhere else: the redraw WOKE an archived thread, so a
        refused shut leaves a finished item's thread open where it was closed before."""
        threads = FakeThreadGateway()
        closed = pr_event("closed", state="closed")
        await build_item_sync(db_sessionmaker, threads, PullRequestPolicy()).sync(closed)
        threads.refuses_every_shut = True

        outcome = await redrawing(
            db_sessionmaker, threads, pull_request=answering(closed)
        ).regenerate(thread_id=threads.created[0].thread_id)

        assert outcome.shut_refused is True


class TestWhatItRefuses:
    async def test_a_thread_this_bot_does_not_track(
        self, registered: Repository, db_sessionmaker: async_sessionmaker, pr_event
    ) -> None:
        threads = FakeThreadGateway()

        with pytest.raises(NotAnItemThreadError):
            await redrawing(
                db_sessionmaker, threads, pull_request=answering(pr_event("opened"))
            ).regenerate(thread_id=999_999)

    async def test_a_board_card_has_nothing_on_github_to_read(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker,
        db_session: AsyncSession,
        pr_event,
    ) -> None:
        """A draft ticket lives on the project board and has no page. The poller already opens a
        replacement for any card whose thread has gone, so there is nothing for this to do."""
        threads = FakeThreadGateway()
        opened = pr_event("opened")
        await build_item_sync(db_sessionmaker, threads, PullRequestPolicy()).sync(opened)
        thread_id = threads.created[0].thread_id
        # The row is a pull request; the service is built knowing only about issues, which is the
        # same shape as meeting a kind it has no reader for.
        service = redrawing(db_sessionmaker, threads, issue=answering(opened))

        with pytest.raises(WorkflowRefusedError, match="board card"):
            await service.regenerate(thread_id=thread_id)

    async def test_a_repository_renamed_away_from_under_it(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker,
        db_session: AsyncSession,
        pr_event,
    ) -> None:
        """A stored name is not an identity. Unchecked, the sync resolves the fetched snapshot by
        its own repository id and rewrites a thread in whichever server registered that name."""
        from dataclasses import replace

        threads = FakeThreadGateway()
        opened = pr_event("opened")
        await build_item_sync(db_sessionmaker, threads, PullRequestPolicy()).sync(opened)
        somebody_else = replace(
            opened,
            repository=replace(
                opened.repository, github_repo_id=opened.repository.github_repo_id + 5000
            ),
        )

        with pytest.raises(RepositoryMismatchError):
            await redrawing(
                db_sessionmaker, threads, pull_request=answering(somebody_else)
            ).regenerate(thread_id=threads.created[0].thread_id)

    async def test_an_item_deleted_on_github(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker,
        db_session: AsyncSession,
        pr_event,
    ) -> None:
        threads = FakeThreadGateway()
        await build_item_sync(db_sessionmaker, threads, PullRequestPolicy()).sync(
            pr_event("opened")
        )

        with pytest.raises(GitHubNotFoundError):
            await redrawing(
                db_sessionmaker, threads, pull_request=refusing(GitHubNotFoundError("gone"))
            ).regenerate(thread_id=threads.created[0].thread_id)

    async def test_a_kind_with_nowhere_to_put_it_says_which_command_fixes_that(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker,
        db_session: AsyncSession,
        pr_event,
    ) -> None:
        """The mapping can be removed after a thread exists, and then the sync has nowhere to
        write. A pull request has no fallback kind, unlike an issue, so this is the shape that
        reaches it."""
        threads = FakeThreadGateway()
        opened = pr_event("opened")
        await build_item_sync(db_sessionmaker, threads, PullRequestPolicy()).sync(opened)
        thread_id = threads.created[0].thread_id
        await db_session.execute(delete(ChannelMapping))
        await db_session.commit()

        with pytest.raises(SyncFailedError, match="/set_channel"):
            await redrawing(db_sessionmaker, threads, pull_request=answering(opened)).regenerate(
                thread_id=thread_id
            )

    async def test_a_sync_that_writes_nothing_is_not_reported_as_success(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker,
        db_session: AsyncSession,
        pr_event,
    ) -> None:
        """The one addition over `ManualSync`, which has this hole. A redraw that silently does
        nothing is the worst answer available: the reason somebody ran it is that the block is
        wrong, and being told it worked stops them looking further."""
        threads = FakeThreadGateway()
        opened = pr_event("opened")
        await build_item_sync(db_sessionmaker, threads, PullRequestPolicy()).sync(opened)
        thread_id = threads.created[0].thread_id
        # A payload from before what is stored is exactly what the staleness guard turns away.
        stale = pr_event("edited", updated_at="2020-01-01T00:00:00Z")

        with pytest.raises(SyncFailedError, match="could not be redrawn"):
            await redrawing(db_sessionmaker, threads, pull_request=answering(stale)).regenerate(
                thread_id=thread_id
            )
