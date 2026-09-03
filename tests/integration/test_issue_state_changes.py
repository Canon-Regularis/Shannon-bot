from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import Repository, TrackedItem
from shannon.discord_bot.errors import DiscordPermissionError
from shannon.domain.enums import Status
from shannon.services.sync.items import ItemSyncService, build_item_sync
from shannon.services.sync.policies import IssuePolicy
from tests.fakes.threads import FakeThreadGateway

pytestmark = pytest.mark.integration

CLOSED = {"state": "closed", "closed_at": "2026-08-11T12:00:00Z"}


async def test_a_lock_the_payload_asked_for_still_fails_the_delivery(
    registered: Repository,
    issue_service: ItemSyncService,
    threads: FakeThreadGateway,
    issue_event,
) -> None:
    """The other side of stepping over a refusal nobody can wait out.

    A pull request's lock is asked for by the row, and the row can be left saying the thread is
    not shut, so granting the permission later is enough on its own and the delivery has nothing
    left to do. An issue's is asked for by the payload, and that delivery is answering something
    somebody just did. Stepping over it would leave the issue reading Closed in the block with an
    open thread underneath and nothing recorded anywhere, so it fails and is recorded as failed.
    """
    await issue_service.sync(issue_event("opened"))
    threads.refuses_every_lock = True

    with pytest.raises(DiscordPermissionError):
        await issue_service.sync(issue_event("closed", **CLOSED))


async def test_a_refused_unlock_does_not_take_the_rest_of_the_delivery_with_it(
    registered: Repository,
    issue_service: ItemSyncService,
    threads: FakeThreadGateway,
    caplog: pytest.LogCaptureFixture,
    db_session: AsyncSession,
    issue_event,
) -> None:
    """Unlocking is the first Discord call a delivery makes, and a permission is permanent.

    So raising here lost everything after it, and lost it on the first attempt rather than over
    two hours: the block was never rewritten and the delivery was dropped. A server that has
    never granted Manage Threads had every reopened issue stop mirroring altogether, which is a
    great deal worse than one that mirrors with a thread that stays shut. Locking makes the same
    bargain and can afford it, being last.
    """
    await issue_service.sync(issue_event("opened"))
    await issue_service.sync(issue_event("closed", **CLOSED))
    threads.refuses_every_lock = True

    with caplog.at_level(logging.WARNING):
        await issue_service.sync(
            issue_event("reopened", title="Reopened after all", updated_at="2026-08-11T13:00:00Z")
        )

    thread = threads.created[0].thread_id
    assert "Reopened after all" in threads.metadata_of(thread), "the delivery was lost entirely"
    assert "so it stays shut" in caplog.text, "it gave up on the thread silently"

    db_session.expire_all()
    item = await db_session.scalar(select(TrackedItem))
    assert item.title == "Reopened after all", "the row never caught up either"


async def test_closing_marks_the_issue_done(
    registered: Repository,
    issue_service: ItemSyncService,
    issue_event,
    db_session: AsyncSession,
) -> None:
    await issue_service.sync(issue_event("opened"))
    await issue_service.sync(issue_event("closed", **CLOSED))

    db_session.expunge_all()
    item = await db_session.scalar(select(TrackedItem))
    assert item is not None
    assert item.status is Status.DONE
    assert item.github_state == "closed"


async def test_closing_locks_the_thread(
    registered: Repository,
    issue_service: ItemSyncService,
    issue_event,
    threads: FakeThreadGateway,
) -> None:
    result = await issue_service.sync(issue_event("opened"))
    assert result is not None
    await issue_service.sync(issue_event("closed", **CLOSED))

    assert threads.locks == [(result.thread_id, True)]
    assert threads.threads[result.thread_id].locked is True


async def test_the_metadata_is_written_before_the_thread_locks(
    registered: Repository,
    issue_service: ItemSyncService,
    issue_event,
    threads: FakeThreadGateway,
) -> None:
    """A locked thread rejects edits, so the order matters rather than being incidental."""
    result = await issue_service.sync(issue_event("opened"))
    assert result is not None

    await issue_service.sync(issue_event("closed", **CLOSED))

    metadata = threads.metadata_of(result.thread_id)
    assert "**State:** Closed" in metadata
    assert "**Status:** DONE" in metadata


async def test_reopening_unlocks_the_thread_and_resets_the_status(
    registered: Repository,
    issue_service: ItemSyncService,
    issue_event,
    threads: FakeThreadGateway,
    db_session: AsyncSession,
) -> None:
    result = await issue_service.sync(issue_event("opened"))
    assert result is not None
    await issue_service.sync(issue_event("closed", **CLOSED))

    await issue_service.sync(issue_event("reopened", state="open", closed_at=None))

    assert threads.locks == [(result.thread_id, True), (result.thread_id, False)]
    assert threads.threads[result.thread_id].locked is False

    db_session.expunge_all()
    item = await db_session.scalar(select(TrackedItem))
    assert item is not None
    assert item.status is Status.NOT_REVIEWED
    assert item.github_state == "open"


async def test_reopening_updates_the_metadata(
    registered: Repository,
    issue_service: ItemSyncService,
    issue_event,
    threads: FakeThreadGateway,
) -> None:
    result = await issue_service.sync(issue_event("opened"))
    assert result is not None
    await issue_service.sync(issue_event("closed", **CLOSED))

    await issue_service.sync(issue_event("reopened", state="open", closed_at=None))

    metadata = threads.metadata_of(result.thread_id)
    assert "**State:** Open" in metadata
    assert "**Status:** NOT_REVIEWED" in metadata


async def test_no_state_change_ever_opens_a_second_thread(
    registered: Repository,
    issue_service: ItemSyncService,
    issue_event,
    threads: FakeThreadGateway,
) -> None:
    await issue_service.sync(issue_event("opened"))
    await issue_service.sync(issue_event("closed", **CLOSED))
    await issue_service.sync(issue_event("reopened", state="open", closed_at=None))

    assert len(threads.created) == 1


async def test_closing_twice_locks_once(
    registered: Repository,
    issue_service: ItemSyncService,
    issue_event,
    threads: FakeThreadGateway,
) -> None:
    """An issue's lock is decided from its payload, so both closes ask for it and only the first
    moves anything. The gateway compares before it edits, so the second costs a lookup and no
    write, which is the cost worth having: the payload is the newest word on whether the thread
    belongs shut, and skipping the ask on a row that says it is already would not notice somebody
    unlocking it by hand.

    Both counted, because they are different questions and the second was written as though it
    answered the first. `locks` records where the lock ended up and only when it moved, so on its
    own it cannot tell one ask from two.
    """
    await issue_service.sync(issue_event("opened"))
    await issue_service.sync(issue_event("closed", **CLOSED))
    await issue_service.sync(issue_event("closed", **CLOSED))

    assert len(threads.locks) == 1, "it edited the thread twice for one close"
    assert threads.lock_calls == [(threads.created[0].thread_id, True)] * 2


async def test_a_locked_thread_still_accepts_metadata_updates(
    registered: Repository,
    issue_service: ItemSyncService,
    issue_event,
    threads: FakeThreadGateway,
) -> None:
    """Closed issues keep receiving label and assignment events, and those have to land."""
    result = await issue_service.sync(issue_event("opened"))
    assert result is not None
    await issue_service.sync(issue_event("closed", **CLOSED))

    await issue_service.sync(issue_event("labeled", labels=[{"name": "wontfix"}], **CLOSED))

    assert "`wontfix`" in threads.metadata_of(result.thread_id)
    assert threads.threads[result.thread_id].locked is True


async def test_locking_never_archives(
    registered: Repository,
    issue_service: ItemSyncService,
    issue_event,
    threads: FakeThreadGateway,
) -> None:
    """Archiving would hide the thread and make every later edit fail."""
    result = await issue_service.sync(issue_event("opened"))
    assert result is not None
    await issue_service.sync(issue_event("closed", **CLOSED))

    assert threads.threads[result.thread_id].archived is False


async def test_an_issue_that_opens_already_closed_is_locked(
    registered: Repository,
    issue_service: ItemSyncService,
    issue_event,
    threads: FakeThreadGateway,
    db_session: AsyncSession,
) -> None:
    """A first sync of an already closed issue, which is what /issue on an old one does."""
    result = await issue_service.sync(issue_event("opened", **CLOSED))

    assert result is not None
    assert threads.threads[result.thread_id].locked is True

    item = await db_session.scalar(select(TrackedItem))
    assert item is not None
    assert item.status is Status.DONE


class TestLockingAfterANewerSyncHasBeenThrough:
    """Only the database half of a sync is ordered; the Discord half can interleave with /pr."""

    async def test_a_superseded_close_does_not_lock_a_reopened_issue(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker,
        threads: FakeThreadGateway,
        issue_event,
    ) -> None:
        service = build_item_sync(db_sessionmaker, threads, IssuePolicy())
        await service.sync(issue_event("opened", updated_at="2026-08-01T10:00:00Z"))
        # The reopen lands while the close is still in its Discord phase.
        await service.sync(
            issue_event("reopened", state="open", closed_at=None, updated_at="2026-08-01T12:00:00Z")
        )

        superseded = issue_event(
            "closed",
            state="closed",
            closed_at="2026-08-01T11:00:00Z",
            updated_at="2026-08-01T11:00:00Z",
        )
        await service.sync(superseded)

        thread = threads.threads[next(iter(threads.threads))]
        assert thread.locked is False, "an open issue was left in a thread nobody can post in"

    async def test_a_reopen_landing_mid_flight_stops_the_close_locking(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker,
        issue_event,
    ) -> None:
        """The other half of the window, and the half the arrival order cannot catch.

        The close above was already stale when it started, so it never got past the database.
        This one is current when it reads and is overtaken while it is talking to Discord.
        Locking is the last step and is decided from what the snapshot said several calls ago,
        so without a second look the reopened issue ends up in a thread nobody can post in. A
        stale metadata block rights itself on the next delivery; a locked thread does not.

        The reopen is written onto the row rather than run as a sync, and neither way of
        running one would reproduce anything. From another task it waits: one at a time is what
        the item's lock buys, and the lock is a Postgres one, so that holds across replicas too.
        From inside this Discord call, which is where this test used to run it, it is the same
        task holding the same item and is let straight through, so it would interleave nothing
        while looking exactly as though it had. What is left is a writer that moves the row
        without going through a sync at all, and a replica still running the build from before
        the lock, which is every rolling deploy while it lasts. Writing it here is those.
        """
        threads = _ReopensMidWrite()
        service = build_item_sync(db_sessionmaker, threads, IssuePolicy())
        await service.sync(issue_event("opened", updated_at="2026-08-01T10:00:00Z"))
        threads.during = lambda: _reopened_where_this_one_cannot_see(db_sessionmaker)

        await service.sync(
            issue_event(
                "closed",
                state="closed",
                closed_at="2026-08-01T12:00:00Z",
                updated_at="2026-08-01T12:00:00Z",
            )
        )

        assert threads.during_ran, "the reopen never landed, so nothing was overtaken"
        thread = threads.threads[next(iter(threads.threads))]
        assert thread.locked is False, "a close that had been overtaken locked the thread anyway"

    async def test_the_newest_close_still_locks(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker,
        threads: FakeThreadGateway,
        issue_event,
    ) -> None:
        service = build_item_sync(db_sessionmaker, threads, IssuePolicy())
        await service.sync(issue_event("opened", updated_at="2026-08-01T10:00:00Z"))

        await service.sync(
            issue_event(
                "closed",
                state="closed",
                closed_at="2026-08-01T12:00:00Z",
                updated_at="2026-08-01T12:00:00Z",
            )
        )

        thread = threads.threads[next(iter(threads.threads))]
        assert thread.locked is True


async def _reopened_where_this_one_cannot_see(sessionmaker: async_sessionmaker) -> None:
    """What a reopen leaves on the row: a later stamp, and the issue open again.

    Only the stamp decides anything, because that is what the second look reads. The status is
    written with it so the row says one thing rather than half of each.
    """
    async with sessionmaker() as session, session.begin():
        item = await session.scalar(select(TrackedItem))
        assert item is not None
        item.github_updated_at = datetime(2026, 8, 1, 13, 0, tzinfo=UTC)
        item.status = Status.NOT_REVIEWED


class _ReopensMidWrite(FakeThreadGateway):
    """Moves the item on from inside a Discord call, which is where the window actually is.

    Once only, or every update it makes would move the item again.
    """

    def __init__(self) -> None:
        super().__init__()
        self.during = None
        self.during_ran = False

    async def update(self, **kwargs):
        handle = await super().update(**kwargs)
        if self.during is not None and not self.during_ran:
            self.during_ran = True
            await self.during()
        return handle
