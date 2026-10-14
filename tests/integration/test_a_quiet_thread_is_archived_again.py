"""A sync built for it puts a quiet thread back the way Discord had it. Issue #185.

Discord archives a thread after a week with nothing in it, and takes no edit into an archived one,
so writing to a quiet thread wakes it. Every delivery and command leaves it awake afterwards, which
is right for them: they are activity. `/refresh` is a sweep over every open item, and waking all
of them would fill the channel with threads nobody has touched in a week, so the sync it is built
with asks first and archives again after.
"""

from __future__ import annotations

import logging

import pytest
from sqlalchemy import update
from sqlalchemy.exc import TimeoutError as NoConnectionToSpare
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import Repository, TrackedItem
from shannon.discord_bot.errors import (
    DiscordGatewayError,
    DiscordPermissionError,
    ThreadNotFoundError,
)
from shannon.domain.enums import Status
from shannon.services.sync.items import ItemSyncService, build_item_sync
from shannon.services.sync.policies import IssuePolicy, PullRequestPolicy
from tests.fakes.threads import FakeThreadGateway

pytestmark = pytest.mark.integration

LATER = "2026-08-10T13:00:00Z"
LATER_STILL = "2026-08-10T14:00:00Z"
ISSUE_LATER = "2026-08-11T10:00:00Z"
ISSUE_LATER_STILL = "2026-08-11T11:00:00Z"


def rearchiving(
    sessionmaker: async_sessionmaker[AsyncSession], threads: FakeThreadGateway, policy=None
) -> ItemSyncService:
    return build_item_sync(
        sessionmaker, threads, policy or PullRequestPolicy(), notifies=False, rearchives=True
    )


async def opened(service: ItemSyncService, event) -> int:
    result = await service.sync(event)
    assert result.thread_id is not None
    return result.thread_id


class _GoneOnceAsked(FakeThreadGateway):
    """Somebody deleted the thread between the question and the write."""

    async def is_archived(self, *, thread_id: int) -> bool:
        archived = await super().is_archived(thread_id=thread_id)
        self.threads.pop(thread_id)
        return archived


class TestAQuietThread:
    async def test_a_pull_requests_is_written_to_and_archived_again(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
        pr_event,
    ) -> None:
        service = rearchiving(db_sessionmaker, threads)
        thread_id = await opened(service, pr_event("opened"))
        threads.go_quiet(thread_id)

        result = await service.sync(pr_event("edited", title="Renamed", updated_at=LATER))

        assert result.synced
        assert "Renamed" in threads.metadata_of(thread_id), "it was archived without the write"
        thread = threads.threads[thread_id]
        assert (thread.archived, thread.locked) == (True, False)
        assert threads.archived_again == [thread_id]

    async def test_an_issues_is_too(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
        issue_event,
    ) -> None:
        """An open issue's sync wakes the thread at the unlock step, before the write - which is
        why the question has to come first."""
        service = rearchiving(db_sessionmaker, threads, IssuePolicy())
        thread_id = await opened(service, issue_event("opened"))
        threads.go_quiet(thread_id)

        await service.sync(issue_event("edited", title="Renamed", updated_at=ISSUE_LATER))

        thread = threads.threads[thread_id]
        assert (thread.archived, thread.locked) == (True, False)
        assert "Renamed" in threads.metadata_of(thread_id)

    async def test_a_done_pull_requests_locked_thread_ends_locked_and_archived(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
        threads: FakeThreadGateway,
        pr_event,
    ) -> None:
        """`/status Done` shuts a pull request's thread before GitHub merges it. Its sync asks the
        thread for nothing, and the write wakes it on its own, so this is the second way in."""
        service = rearchiving(db_sessionmaker, threads)
        thread_id = await opened(service, pr_event("opened"))
        await db_session.execute(
            update(TrackedItem).values(status=Status.DONE, discord_thread_locked=True)
        )
        await db_session.commit()
        threads.threads[thread_id].locked = True
        threads.threads[thread_id].archived = True

        await service.sync(pr_event("edited", title="Renamed", updated_at=LATER))

        thread = threads.threads[thread_id]
        assert (thread.archived, thread.locked) == (True, True)
        assert "Renamed" in threads.metadata_of(thread_id)


class TestAWriteThatFailsAfterTheWake:
    async def test_the_thread_is_still_archived_again(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
        pr_event,
    ) -> None:
        """The unlock step has woken it before the write is refused. The failure still goes up
        to the caller, and the thread goes back where it was, or it would sit in the channel
        until Discord archived it again in a week - and the next sync would find it awake."""
        service = rearchiving(db_sessionmaker, threads)
        thread_id = await opened(service, pr_event("opened"))
        threads.go_quiet(thread_id)
        threads.fail_next_update = True

        with pytest.raises(DiscordGatewayError):
            await service.sync(pr_event("edited", title="Renamed", updated_at=LATER))

        thread = threads.threads[thread_id]
        assert (thread.archived, thread.locked) == (True, False)
        assert threads.archived_again == [thread_id]


class TestWhatIsLeftAwake:
    async def test_a_thread_that_was_awake_is_not_archived(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
        pr_event,
    ) -> None:
        service = rearchiving(db_sessionmaker, threads)
        thread_id = await opened(service, pr_event("opened"))

        await service.sync(pr_event("edited", title="Renamed", updated_at=LATER))

        assert threads.asked_if_archived == [thread_id]
        assert threads.archive_calls == []
        assert threads.threads[thread_id].archived is False

    async def test_opening_a_thread_asks_nothing(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
        pr_event,
    ) -> None:
        await opened(rearchiving(db_sessionmaker, threads), pr_event("opened"))

        assert threads.asked_if_archived == []
        assert threads.archive_calls == []

    async def test_a_replacement_for_a_deleted_thread_is_left_awake(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        pr_event,
    ) -> None:
        """Archived when asked, gone by the write: the item gets a new thread, and a new thread
        was never quiet."""
        threads = _GoneOnceAsked()
        service = rearchiving(db_sessionmaker, threads)
        thread_id = await opened(service, pr_event("opened"))
        threads.go_quiet(thread_id)

        result = await service.sync(pr_event("edited", title="Renamed", updated_at=LATER))

        assert result.created
        assert result.thread_id is not None and result.thread_id != thread_id
        assert threads.archive_calls == []
        assert threads.threads[result.thread_id].archived is False

    async def test_a_thread_handed_back_because_the_item_reopened_is_left_awake(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
        issue_event,
    ) -> None:
        """This bot shut it when the issue closed. The issue reopening is news, and a delivery
        saying so would leave the thread awake, so this does too."""
        service = rearchiving(db_sessionmaker, threads, IssuePolicy())
        thread_id = await opened(service, issue_event("opened"))
        await service.sync(
            issue_event(
                "closed",
                state="closed",
                closed_at="2026-08-11T09:45:00Z",
                updated_at=ISSUE_LATER,
            )
        )
        assert threads.threads[thread_id].archived is True, "arranged wrong"

        await service.sync(issue_event("reopened", updated_at=ISSUE_LATER_STILL))

        thread = threads.threads[thread_id]
        assert (thread.archived, thread.locked) == (False, False)
        assert threads.archive_calls == []

    async def test_one_handed_back_stays_awake_when_the_row_cannot_be_told(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
        issue_event,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Discord has the thread back before the row hears of it. A row write that fails then
        fails the sync, and the thread stays the way Discord has it, rather than being archived
        again on the way out as if nothing had handed it back."""
        service = rearchiving(db_sessionmaker, threads, IssuePolicy())
        thread_id = await opened(service, issue_event("opened"))
        await service.sync(
            issue_event(
                "closed",
                state="closed",
                closed_at="2026-08-11T09:45:00Z",
                updated_at=ISSUE_LATER,
            )
        )
        assert threads.threads[thread_id].archived is True, "arranged wrong"

        async def no_connection_to_spare(*_: object, **__: object) -> None:
            raise NoConnectionToSpare("QueuePool limit of size 25 overflow 10 reached")

        monkeypatch.setattr(service, "_note_the_lock", no_connection_to_spare)

        with pytest.raises(NoConnectionToSpare):
            await service.sync(issue_event("reopened", updated_at=ISSUE_LATER_STILL))

        thread = threads.threads[thread_id]
        assert (thread.archived, thread.locked) == (False, False)
        assert threads.archive_calls == []

    @pytest.mark.parametrize(
        "answer",
        [
            DiscordPermissionError("Discord will not let the bot archive the thread"),
            DiscordGatewayError("Discord did not answer"),
            ThreadNotFoundError("somebody deleted the thread just after the write"),
        ],
        ids=["refused", "an outage", "deleted since"],
    )
    async def test_an_archive_that_does_not_land_is_logged_and_the_sync_still_lands(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
        pr_event,
        caplog: pytest.LogCaptureFixture,
        answer: Exception,
    ) -> None:
        """The block is in by then. A thread left awake is one Discord archives again after
        another quiet week, which is not worth failing a write that worked - whatever the reason
        the archive did not land."""
        service = rearchiving(db_sessionmaker, threads)
        thread_id = await opened(service, pr_event("opened"))
        threads.go_quiet(thread_id)
        threads.archive_error = answer

        with caplog.at_level(logging.WARNING, logger="shannon.services.sync.items"):
            result = await service.sync(pr_event("edited", title="Renamed", updated_at=LATER))

        assert result.synced
        assert "Renamed" in threads.metadata_of(thread_id)
        assert "could not archive the thread" in caplog.text
        assert threads.threads[thread_id].archived is False


class TestEverythingElseThatWrites:
    async def test_the_ordinary_sync_leaves_a_quiet_thread_awake_and_asks_nothing(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
        pr_event,
    ) -> None:
        """Deliveries and commands are activity, so their thread is right to wake, and asking
        about it would be a Discord call for nothing."""
        service = build_item_sync(db_sessionmaker, threads, PullRequestPolicy())
        thread_id = await opened(service, pr_event("opened"))
        threads.go_quiet(thread_id)

        await service.sync(pr_event("edited", title="Renamed", updated_at=LATER_STILL))

        assert threads.threads[thread_id].archived is False
        assert threads.asked_if_archived == []
        assert threads.archive_calls == []
