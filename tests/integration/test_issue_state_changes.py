from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import Repository, TrackedItem
from shannon.domain.enums import Status
from shannon.services.sync.items import ItemSyncService, build_item_sync
from shannon.services.sync.policies import IssuePolicy
from tests.fakes.threads import FakeThreadGateway

pytestmark = pytest.mark.integration

CLOSED = {"state": "closed", "closed_at": "2026-08-11T12:00:00Z"}


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
    """The second close finds the thread already locked, and a redundant edit is a wasted call."""
    await issue_service.sync(issue_event("opened"))
    await issue_service.sync(issue_event("closed", **CLOSED))
    await issue_service.sync(issue_event("closed", **CLOSED))

    assert len(threads.locks) == 1


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
