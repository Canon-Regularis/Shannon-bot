from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import Repository, TrackedItem
from shannon.db.stores.tracked_items import TrackedItemStore
from shannon.discord_bot.errors import ThreadStartedEmptyError
from shannon.discord_bot.threads import ThreadHandle
from shannon.domain.errors import ItemNotReadyError
from shannon.github.webhooks.comments import parse_comment_event
from shannon.services.item_sync import ItemSyncService
from shannon.services.notes import ItemNoteMirror
from shannon.services.policies import IssuePolicy, PullRequestPolicy
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads

pytestmark = pytest.mark.integration


@pytest.fixture
def service(db_sessionmaker: async_sessionmaker, threads: FakeThreadGateway) -> ItemSyncService:
    return ItemSyncService(db_sessionmaker, threads, PullRequestPolicy())


async def stored(session: AsyncSession) -> TrackedItem:
    session.expire_all()
    item = await session.scalar(select(TrackedItem))
    assert item is not None
    return item


class TestADeletedThread:
    """Somebody tidying a channel must not silence an item for the rest of its life."""

    async def test_the_item_gets_a_replacement_thread(
        self,
        registered: Repository,
        service: ItemSyncService,
        threads: FakeThreadGateway,
        db_session: AsyncSession,
        pr_event,
    ) -> None:
        first = await service.sync(pr_event("opened"))
        threads.threads.pop(first.thread_id)

        second = await service.sync(pr_event("edited", title="Renamed"))

        assert second.synced
        assert second.thread_id != first.thread_id
        assert second.created is True
        assert (await stored(db_session)).discord_thread_id == second.thread_id

    async def test_the_replacement_carries_the_current_metadata(
        self,
        registered: Repository,
        service: ItemSyncService,
        threads: FakeThreadGateway,
        pr_event,
    ) -> None:
        first = await service.sync(pr_event("opened"))
        threads.threads.pop(first.thread_id)

        second = await service.sync(pr_event("edited", title="Renamed"))

        assert "Renamed" in threads.metadata_of(second.thread_id)

    async def test_later_events_go_to_the_replacement(
        self,
        registered: Repository,
        service: ItemSyncService,
        threads: FakeThreadGateway,
        pr_event,
    ) -> None:
        first = await service.sync(pr_event("opened"))
        threads.threads.pop(first.thread_id)
        second = await service.sync(pr_event("edited"))

        third = await service.sync(pr_event("edited", title="Again"))

        assert third.thread_id == second.thread_id
        assert third.created is False


class TestAnArchivedThread:
    """Discord archives a quiet thread on its own, and then refuses every write to it."""

    async def test_an_update_reopens_it_rather_than_failing(
        self,
        registered: Repository,
        service: ItemSyncService,
        threads: FakeThreadGateway,
        pr_event,
    ) -> None:
        first = await service.sync(pr_event("opened"))
        threads.threads[first.thread_id].archived = True

        second = await service.sync(pr_event("edited", title="Still moving"))

        assert second.synced
        assert second.thread_id == first.thread_id
        assert threads.unarchived == [first.thread_id]
        assert "Still moving" in threads.metadata_of(first.thread_id)


class TestAThreadThatWasOpenedButNotWrittenTo:
    """Opening the thread and posting in it are two calls, and the second one can fail."""

    async def test_the_thread_id_is_recorded_before_the_failure_surfaces(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker,
        db_session: AsyncSession,
        pr_event,
    ) -> None:
        threads = _EmptyOnFirstCreate()
        service = ItemSyncService(db_sessionmaker, threads, PullRequestPolicy())

        with pytest.raises(ThreadStartedEmptyError):
            await service.sync(pr_event("opened"))

        assert (await stored(db_session)).discord_thread_id == threads.opened[0]

    async def test_the_retry_writes_into_it_rather_than_opening_another(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker,
        pr_event,
    ) -> None:
        threads = _EmptyOnFirstCreate()
        service = ItemSyncService(db_sessionmaker, threads, PullRequestPolicy())
        with pytest.raises(ThreadStartedEmptyError):
            await service.sync(pr_event("opened"))

        result = await service.sync(pr_event("opened"))

        assert result.thread_id == threads.opened[0]
        assert len(threads.opened) == 1
        # The metadata the first attempt never managed to post is there now.
        assert "Add the webhook endpoint" in threads.metadata_of(result.thread_id)


class TestTwoSyncsRacingToOpenAThread:
    """The worker and /pr both reach an item with no thread, and both call Discord."""

    async def test_only_one_thread_stays_attached(
        self,
        registered: Repository,
        service: ItemSyncService,
        threads: FakeThreadGateway,
        db_session: AsyncSession,
        pr_event,
    ) -> None:
        results = await asyncio.gather(
            service.sync(pr_event("opened")),
            service.sync(pr_event("labeled")),
            return_exceptions=True,
        )

        failures = [r for r in results if isinstance(r, BaseException)]
        assert failures == [], f"a racing sync raised: {failures}"
        assert len({r.thread_id for r in results}) == 1
        assert (await stored(db_session)).discord_thread_id == results[0].thread_id

    async def test_the_thread_that_lost_is_cleaned_up(
        self,
        registered: Repository,
        service: ItemSyncService,
        threads: FakeThreadGateway,
        pr_event,
    ) -> None:
        results = await asyncio.gather(
            service.sync(pr_event("opened")),
            service.sync(pr_event("labeled")),
        )

        # Whichever lost was removed, so the channel holds exactly the one that won.
        assert len(threads.threads) == 1
        assert list(threads.threads) == [results[0].thread_id]
        assert len(threads.deleted) == 1

    async def test_a_burst_leaves_one_thread(
        self,
        registered: Repository,
        service: ItemSyncService,
        threads: FakeThreadGateway,
        pr_event,
    ) -> None:
        await asyncio.gather(
            *(service.sync(pr_event("edited", title=f"Title {n}")) for n in range(6))
        )

        assert len(threads.threads) == 1


class _EmptyOnFirstCreate(FakeThreadGateway):
    """A gateway whose first thread opens but whose first message does not land."""

    def __init__(self) -> None:
        super().__init__()
        self.opened: list[int] = []

    async def create(self, *, channel_id: int, name: str, content: str) -> ThreadHandle:
        handle = await super().create(channel_id=channel_id, name=name, content=content)
        if not self.opened:
            self.opened.append(handle.thread_id)
            thread = self.threads[handle.thread_id]
            thread.messages.clear()
            thread.metadata_message_id = None
            raise ThreadStartedEmptyError(
                "Discord refused the first message", thread_id=handle.thread_id
            )
        return handle


class TestAnIssueWhoseThreadWasDeleted:
    """An open issue always unlocks before writing, so the unlock reaches the dead thread first."""

    async def test_the_issue_gets_a_replacement_thread(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker,
        threads: FakeThreadGateway,
        issue_event,
    ) -> None:
        service = ItemSyncService(db_sessionmaker, threads, IssuePolicy())
        first = await service.sync(issue_event("opened"))
        threads.threads.pop(first.thread_id)

        second = await service.sync(issue_event("edited", title="Still open"))

        assert second.synced
        assert second.thread_id != first.thread_id
        assert "Still open" in threads.metadata_of(second.thread_id)


class TestANoteOnADeletedThread:
    """The note mirror sits on the other side of the same boundary as the item sync."""

    @pytest.fixture
    def mirror(
        self, db_sessionmaker: async_sessionmaker, threads: FakeThreadGateway
    ) -> ItemNoteMirror:
        return ItemNoteMirror(db_sessionmaker, threads, render=lambda note, mentions: "hello")

    @pytest.fixture
    def issues(
        self, db_sessionmaker: async_sessionmaker, threads: FakeThreadGateway
    ) -> ItemSyncService:
        return ItemSyncService(db_sessionmaker, threads, IssuePolicy())

    async def test_the_dead_pointer_is_dropped_and_the_note_asks_to_be_retried(
        self,
        registered: Repository,
        issues: ItemSyncService,
        mirror: ItemNoteMirror,
        db_session: AsyncSession,
        threads: FakeThreadGateway,
        issue_event,
    ) -> None:
        synced = await issues.sync(issue_event("opened"))
        threads.threads.pop(synced.thread_id)

        with pytest.raises(ItemNotReadyError):
            await mirror.mirror(parse_comment_event("created", payloads.issue_comment_event()))

        assert (await stored(db_session)).discord_thread_id is None

    async def test_the_next_item_event_rebuilds_and_the_note_then_lands(
        self,
        registered: Repository,
        issues: ItemSyncService,
        mirror: ItemNoteMirror,
        threads: FakeThreadGateway,
        issue_event,
    ) -> None:
        synced = await issues.sync(issue_event("opened"))
        threads.threads.pop(synced.thread_id)
        with pytest.raises(ItemNotReadyError):
            await mirror.mirror(parse_comment_event("created", payloads.issue_comment_event()))

        rebuilt = await issues.sync(issue_event("edited"))
        posted = await mirror.mirror(parse_comment_event("created", payloads.issue_comment_event()))

        assert posted is True
        assert threads.posts == [(rebuilt.thread_id, "hello")]

    async def test_a_pointer_that_has_already_moved_on_is_left_alone(
        self,
        registered: Repository,
        issues: ItemSyncService,
        db_sessionmaker: async_sessionmaker,
        db_session: AsyncSession,
        threads: FakeThreadGateway,
        issue_event,
    ) -> None:
        """A note a step behind must not strand the thread a concurrent sync just rebuilt."""
        synced = await issues.sync(issue_event("opened"))
        threads.threads.pop(synced.thread_id)
        rebuilt = await issues.sync(issue_event("edited"))

        async with db_sessionmaker() as session, session.begin():
            cleared = await TrackedItemStore(session).forget_thread(
                rebuilt.tracked_item_id, dead_thread_id=synced.thread_id
            )

        assert cleared is False
        assert (await stored(db_session)).discord_thread_id == rebuilt.thread_id
