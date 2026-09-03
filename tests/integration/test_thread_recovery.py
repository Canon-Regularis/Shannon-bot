from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from shannon.db.models import Repository, TrackedItem
from shannon.db.stores.thread_pointers import ThreadPointerStore
from shannon.discord_bot.errors import DiscordGatewayError, ThreadStartedEmptyError
from shannon.discord_bot.threads import ThreadHandle
from shannon.domain.enums import Status
from shannon.domain.errors import ItemNotReadyError, PermanentError
from shannon.github.webhooks.comments import parse_comment_event
from shannon.services.notes import ItemNoteMirror
from shannon.services.sync.items import ItemSyncService, SyncOutcome, build_item_sync
from shannon.services.sync.policies import IssuePolicy, PullRequestPolicy
from shannon.services.sync.threads import ItemThreads, ThreadTarget
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads
from tests.support.stack import build_stack

pytestmark = pytest.mark.integration


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
        sync_service: ItemSyncService,
        threads: FakeThreadGateway,
        db_session: AsyncSession,
        pr_event,
    ) -> None:
        first = await sync_service.sync(pr_event("opened"))
        threads.threads.pop(first.thread_id)

        second = await sync_service.sync(pr_event("edited", title="Renamed"))

        assert second.synced
        assert second.thread_id != first.thread_id
        assert second.created is True
        assert (await stored(db_session)).discord_thread_id == second.thread_id

    async def test_the_replacement_carries_the_current_metadata(
        self,
        registered: Repository,
        sync_service: ItemSyncService,
        threads: FakeThreadGateway,
        pr_event,
    ) -> None:
        first = await sync_service.sync(pr_event("opened"))
        threads.threads.pop(first.thread_id)

        second = await sync_service.sync(pr_event("edited", title="Renamed"))

        assert "Renamed" in threads.metadata_of(second.thread_id)

    async def test_later_events_go_to_the_replacement(
        self,
        registered: Repository,
        sync_service: ItemSyncService,
        threads: FakeThreadGateway,
        pr_event,
    ) -> None:
        first = await sync_service.sync(pr_event("opened"))
        threads.threads.pop(first.thread_id)
        second = await sync_service.sync(pr_event("edited"))

        third = await sync_service.sync(pr_event("edited", title="Again"))

        assert third.thread_id == second.thread_id
        assert third.created is False


class TestAnArchivedThread:
    """Discord archives a quiet thread on its own, and then refuses every write to it."""

    async def test_an_update_reopens_it_rather_than_failing(
        self,
        registered: Repository,
        sync_service: ItemSyncService,
        threads: FakeThreadGateway,
        pr_event,
    ) -> None:
        first = await sync_service.sync(pr_event("opened"))
        threads.threads[first.thread_id].archived = True

        second = await sync_service.sync(pr_event("edited", title="Still moving"))

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
        service = build_item_sync(db_sessionmaker, threads, PullRequestPolicy())

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
        service = build_item_sync(db_sessionmaker, threads, PullRequestPolicy())
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
        sync_service: ItemSyncService,
        threads: FakeThreadGateway,
        db_session: AsyncSession,
        pr_event,
    ) -> None:
        results = await asyncio.gather(
            sync_service.sync(pr_event("opened")),
            sync_service.sync(pr_event("labeled")),
            return_exceptions=True,
        )

        failures = [r for r in results if isinstance(r, BaseException)]
        assert failures == [], f"a racing sync raised: {failures}"
        assert len({r.thread_id for r in results}) == 1
        assert (await stored(db_session)).discord_thread_id == results[0].thread_id

    async def test_no_second_thread_is_opened_to_be_cleaned_up(
        self,
        registered: Repository,
        sync_service: ItemSyncService,
        threads: FakeThreadGateway,
        pr_event,
    ) -> None:
        """Two syncs of one item are held to one at a time now, so the second finds a thread
        already there and writes to it rather than opening a second one and losing.

        This used to open two and remove the loser, which was the right answer to the wrong
        question: the thread was created in Discord and deleted again, and anybody watching the
        channel saw it appear and go. The claim that removes a loser is still there and still
        right, because nothing should depend on a lock never being missed, but it is not the
        ordinary path any more.
        """
        results = await asyncio.gather(
            sync_service.sync(pr_event("opened")),
            sync_service.sync(pr_event("labeled")),
        )

        assert len(threads.threads) == 1
        assert list(threads.threads) == [results[0].thread_id]
        assert threads.deleted == [], "a thread was opened only to be taken away again"
        assert len(threads.created) == 1, "two threads were opened for one item"

    async def test_the_loser_is_still_taken_away_when_something_else_claimed_first(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker,
        db_session: AsyncSession,
        sync_service: ItemSyncService,
        threads: FakeThreadGateway,
        pr_event,
    ) -> None:
        """The guard above, reached on its own, since no sync can reach it any more.

        Two syncs of one item take turns now, and the lock is a Postgres one, so a second replica
        takes its turn as well. What is left is a replica running a build from before the lock,
        which is every rolling deploy while it lasts, and the guard is here for that.

        `ItemThreads` is driven straight rather than through a sync, because a target saying the
        item has no thread while the row says it has one is the whole of the race and there is no
        longer any way to make a sync hold that view. Not a weaker test for it: the claim is
        where the decision is made, and this is the claim losing.
        """
        await sync_service.sync(pr_event("opened"))
        item = await stored(db_session)
        won = item.discord_thread_id

        # What the losing sync believed when it started: an item with no thread at all.
        wrote = await ItemThreads(db_sessionmaker, threads).write(
            ThreadTarget(
                tracked_item_id=item.id,
                channel_id=item.discord_channel_id,
                thread_id=None,
                message_id=None,
            ),
            name="#7 Add the webhook endpoint",
            content="metadata",
        )

        assert wrote.handle.thread_id == won, "the loser overwrote the thread that had won"
        assert threads.deleted, "the thread that lost was left in the channel for ever"
        assert threads.deleted != [won], "the thread that won was the one taken away"
        assert (await stored(db_session)).discord_thread_id == won

    async def test_a_burst_leaves_one_thread(
        self,
        registered: Repository,
        sync_service: ItemSyncService,
        threads: FakeThreadGateway,
        pr_event,
    ) -> None:
        await asyncio.gather(
            *(sync_service.sync(pr_event("edited", title=f"Title {n}")) for n in range(6))
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
        service = build_item_sync(db_sessionmaker, threads, IssuePolicy())
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
        return build_item_sync(db_sessionmaker, threads, IssuePolicy())

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
            cleared = await ThreadPointerStore(session).forget_thread(
                rebuilt.tracked_item_id, dead_thread_id=synced.thread_id
            )

        assert cleared is False
        assert (await stored(db_session)).discord_thread_id == rebuilt.thread_id


class TestARebuildWhoseSlotWasClearedUnderIt:
    """The note mirror lets go of a dead thread while a sync may be rebuilding the same item."""

    async def test_the_replacement_is_kept_rather_than_destroyed(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker,
        db_session: AsyncSession,
        threads: FakeThreadGateway,
        pr_event,
    ) -> None:
        service = build_item_sync(db_sessionmaker, threads, PullRequestPolicy())
        first = await service.sync(pr_event("opened"))
        threads.threads.pop(first.thread_id)
        # Somebody else notices the thread is gone and lets go of it first.
        async with db_sessionmaker() as session, session.begin():
            await ThreadPointerStore(session).forget_thread(
                first.tracked_item_id, dead_thread_id=first.thread_id
            )

        second = await service.sync(pr_event("edited", title="Rebuilt"))

        assert second.thread_id is not None
        assert second.thread_id in threads.threads
        assert (await stored(db_session)).discord_thread_id == second.thread_id

    async def test_the_item_is_never_left_holding_nothing(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker,
        db_session: AsyncSession,
        threads: FakeThreadGateway,
        pr_event,
    ) -> None:
        """Reporting SYNCED with no thread would finish the delivery and lose the event."""
        service = build_item_sync(db_sessionmaker, threads, PullRequestPolicy())
        first = await service.sync(pr_event("opened"))
        threads.threads.pop(first.thread_id)
        async with db_sessionmaker() as session, session.begin():
            await ThreadPointerStore(session).forget_thread(
                first.tracked_item_id, dead_thread_id=first.thread_id
            )

        second = await service.sync(pr_event("edited"))

        assert second.synced
        assert second.thread_id is not None
        assert len(threads.threads) == 1


class TestTheStalenessWatermark:
    async def test_an_old_snapshot_applied_without_a_thread_does_not_lower_it(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker,
        db_session: AsyncSession,
        threads: FakeThreadGateway,
        pr_event,
    ) -> None:
        """An item with no thread is never stale, so an old snapshot can still be applied."""
        service = build_item_sync(db_sessionmaker, threads, PullRequestPolicy())
        newest = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
        await service.sync(pr_event("edited", updated_at=newest.isoformat()))
        first = await stored(db_session)
        threads.threads.pop(first.discord_thread_id)
        async with db_sessionmaker() as session, session.begin():
            await ThreadPointerStore(session).forget_thread(
                first.id, dead_thread_id=first.discord_thread_id
            )

        older = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
        await service.sync(pr_event("edited", updated_at=older.isoformat()))

        assert (await stored(db_session)).github_updated_at == newest

    async def test_a_newer_snapshot_still_advances_it(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker,
        db_session: AsyncSession,
        threads: FakeThreadGateway,
        pr_event,
    ) -> None:
        service = build_item_sync(db_sessionmaker, threads, PullRequestPolicy())
        await service.sync(pr_event("opened"))
        newer = datetime(2026, 12, 25, 9, 0, tzinfo=UTC)

        await service.sync(pr_event("edited", updated_at=newer.isoformat()))

        assert (await stored(db_session)).github_updated_at == newer


class TestBeingOutOfTheServerForAWhile:
    """An admin removes the bot and puts it back, or re-authorises the integration.

    While it is out, discord.py empties the guild from its cache, so every call falls through to
    a fetch and Discord answers for a channel it can no longer see with the same refusal it gives
    for one the bot is not allowed to touch. Filed as a missing permission that is permanent, and
    the worker drops a permanent failure on its first attempt, so every delivery in the window was
    lost. The row is committed before any of it, so the item was left saying one thing while its
    thread said another, and for an item that gets no further event, a just-closed issue or a
    just-merged pull request, nothing was coming to correct it.

    The sixteen attempts over two hours exist for exactly this kind of absence.
    """

    async def test_a_refusal_while_out_of_the_server_is_worth_waiting_out(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker,
        threads: FakeThreadGateway,
        issue_event,
    ) -> None:
        issues = build_item_sync(db_sessionmaker, threads, IssuePolicy())
        await issues.sync(issue_event("opened"))

        threads.refuses_every_update = True
        threads.removed_from.add(registered.discord_guild_id)

        with pytest.raises(DiscordGatewayError) as caught:
            await issues.sync(issue_event("edited", updated_at="2026-08-11T13:00:00Z"))

        assert not isinstance(caught.value, PermanentError), (
            "a five minute absence was dropped on the first attempt"
        )

    async def test_a_permission_it_was_never_given_still_fails_at_once(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker,
        threads: FakeThreadGateway,
        issue_event,
    ) -> None:
        """The other side of it, and the reason the two must be told apart. Nobody grants a
        permission by waiting, so retrying one for two hours is two hours of nothing."""
        issues = build_item_sync(db_sessionmaker, threads, IssuePolicy())
        await issues.sync(issue_event("opened"))

        threads.refuses_every_update = True

        with pytest.raises(PermanentError):
            await issues.sync(issue_event("edited", updated_at="2026-08-11T13:00:00Z"))

    async def test_a_lock_a_late_delivery_still_owes_is_not_stepped_over_while_out(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker,
        db_session: AsyncSession,
        threads: FakeThreadGateway,
        issue_event,
    ) -> None:
        """The same absence, on the one step that answers a refusal by carrying on regardless.

        A delivery turned away as stale can still owe the lock, and that step steps over a
        refusal on purpose: a permission nobody has granted must not fail one delivery sixteen
        times for something no amount of waiting fixes. It is the wrong answer for a bot that
        has been removed, and it is worse here than anywhere else. This is only ever reached for
        an item that is finished, which sends no further event, and the delivery is reported
        handled either way, so the thread is left open above a block reading DONE with nothing
        coming back for it.
        """
        # Read before anything expires it: `stored` clears this session, and the repository is
        # in it.
        guild_id = registered.discord_guild_id
        issues = build_item_sync(db_sessionmaker, threads, IssuePolicy())
        await issues.sync(issue_event("opened"))
        await issues.sync(
            issue_event(
                "closed",
                state="closed",
                closed_at="2026-08-11T12:00:00Z",
                updated_at="2026-08-11T12:00:00Z",
            )
        )

        # Somebody deletes the thread, and a delivery from before the close rebuilds it. The
        # lock failing on the way is what leaves the item owing one.
        first = threads.created[0].thread_id
        threads.threads.pop(first)
        item = await stored(db_session)
        async with db_sessionmaker() as session, session.begin():
            await ThreadPointerStore(session).forget_thread(item.id, dead_thread_id=first)

        stale = issue_event("edited")
        threads.fail_next_lock = True
        with pytest.raises(DiscordGatewayError):
            await issues.sync(stale)

        rebuilt = threads.created[-1].thread_id
        assert rebuilt != first, "nothing was rebuilt, so this proves nothing"
        assert threads.threads[rebuilt].locked is False, "the lock landed, so this proves nothing"

        threads.refuses_every_lock = True
        threads.removed_from.add(guild_id)

        with pytest.raises(DiscordGatewayError) as caught:
            await issues.sync(stale)

        assert not isinstance(caught.value, PermanentError), (
            "a lock a finished item was owed was stepped over and never asked for again"
        )
        assert threads.threads[rebuilt].locked is False


class TestTheChannelUnderneathBeingDeleted:
    """Deleting a channel deletes every thread in it, and Discord reports each one only while
    discord.py still has that thread cached. It drops one the moment the thread archives, so the
    live threads announce themselves and the quiet ones do not.

    The quiet ones are the whole reason any of this exists. A pull request or an issue is rebuilt
    by its next webhook whether or not the pointer was cleared. A draft card parked in a column
    nobody touches has no webhook and no visitor but the poller, which decides from a stored
    pointer without asking Discord, so it was mirrored nowhere for good and said nothing.
    """

    async def test_every_thread_that_was_in_it_is_let_go_of(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker,
        db_session: AsyncSession,
        threads: FakeThreadGateway,
        issue_event,
    ) -> None:
        issues = build_item_sync(db_sessionmaker, threads, IssuePolicy())
        await issues.sync(issue_event("opened"))
        # Where Discord says the thread is, which is where this comes from in production: the
        # listener is handed the channel that was deleted and knows nothing of the row. Taking
        # it off the column instead asks the column to agree with itself, and would pass just as
        # well with the wrong channel written there.
        channel_id = threads.created[0].channel_id
        item = await stored(db_session)
        assert item.discord_channel_id == channel_id, "the row recorded the wrong channel"

        async with db_sessionmaker() as session, session.begin():
            forgotten = await ThreadPointerStore(session).forget_channel(channel_id)

        assert forgotten == [item.id]
        db_session.expire_all()
        after = await stored(db_session)
        assert after.discord_thread_id is None, "it kept pointing at a thread that is gone"
        assert after.discord_channel_id is None

    async def test_a_channel_holding_nothing_of_ours_is_left_alone(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker,
        db_session: AsyncSession,
        threads: FakeThreadGateway,
        issue_event,
    ) -> None:
        """Most of the channels Discord reports are nobody's business here."""
        issues = build_item_sync(db_sessionmaker, threads, IssuePolicy())
        await issues.sync(issue_event("opened"))

        async with db_sessionmaker() as session, session.begin():
            forgotten = await ThreadPointerStore(session).forget_channel(999_999)

        assert forgotten == []
        assert (await stored(db_session)).discord_thread_id is not None

    async def test_a_thread_whose_channel_nobody_recorded_is_left_alone(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker,
        db_session: AsyncSession,
        threads: FakeThreadGateway,
        issue_event,
    ) -> None:
        """Every row written before the channel was recorded has none. Not knowing where a thread
        is is not the same as knowing it was in the channel that went, and letting go of a thread
        that is perfectly fine opens a second one beside it."""
        issues = build_item_sync(db_sessionmaker, threads, IssuePolicy())
        await issues.sync(issue_event("opened"))
        item = await stored(db_session)
        channel = item.discord_channel_id
        await db_session.execute(
            update(TrackedItem).where(TrackedItem.id == item.id).values(discord_channel_id=None)
        )
        await db_session.commit()

        async with db_sessionmaker() as session, session.begin():
            forgotten = await ThreadPointerStore(session).forget_channel(channel)

        assert forgotten == []
        db_session.expire_all()
        assert (await stored(db_session)).discord_thread_id is not None


class TestAStaleDeliveryThatRebuiltTheThread:
    """A delivery from before the close is the only thing left to rebuild a closed issue.

    Nothing else is coming: a closed issue sends no more item events, and the note path needs a
    comment. So when an old delivery is the one that finds the thread gone, it is the recovery.

    It rebuilds the thread and then owes it a lock, decided from the row because its own payload
    is out of date. Attaching the thread is committed before any of the Discord work that
    follows, so the attempt that rebuilds also arms the staleness guard against its own retry.
    One 503 on the lock and every retry was turned away as superseded, reported as handled, and
    the owed lock dropped: a closed issue with a thread anybody could reply in, above a block
    reading Closed, for good.
    """

    @pytest.fixture
    def issues(
        self, db_sessionmaker: async_sessionmaker, threads: FakeThreadGateway
    ) -> ItemSyncService:
        return build_item_sync(db_sessionmaker, threads, IssuePolicy())

    async def test_the_retry_is_not_turned_away_while_the_lock_is_owed(
        self,
        registered: Repository,
        issues: ItemSyncService,
        db_sessionmaker: async_sessionmaker,
        db_session: AsyncSession,
        threads: FakeThreadGateway,
        issue_event,
    ) -> None:
        await issues.sync(issue_event("opened"))
        await issues.sync(
            issue_event(
                "closed",
                state="closed",
                closed_at="2026-08-11T12:00:00Z",
                updated_at="2026-08-11T12:00:00Z",
            )
        )
        first = threads.created[0].thread_id
        assert threads.threads[first].locked is True, "the close never locked it"

        # Somebody deletes the thread and the gateway listener lets go of the pointer.
        threads.threads.pop(first)
        item = await stored(db_session)
        async with db_sessionmaker() as session, session.begin():
            await ThreadPointerStore(session).forget_thread(item.id, dead_thread_id=first)

        # A delivery from before the close, still in the queue. It rebuilds, and the lock fails.
        stale = issue_event("edited")
        threads.fail_next_lock = True
        with pytest.raises(DiscordGatewayError):
            await issues.sync(stale)

        rebuilt = threads.created[-1].thread_id
        assert rebuilt != first, "nothing was rebuilt, so this proves nothing"
        assert threads.threads[rebuilt].locked is False, "the lock landed, so this proves nothing"

        await issues.sync(stale)

        assert threads.threads[rebuilt].locked is True, "the retry was turned away as stale"

    async def test_it_settles_the_lock_and_nothing_else(
        self,
        registered: Repository,
        issues: ItemSyncService,
        db_sessionmaker: async_sessionmaker,
        db_session: AsyncSession,
        threads: FakeThreadGateway,
        issue_event,
    ) -> None:
        """The lock is the only thing a delivery this old is still right about.

        Letting it through to the write instead reaches the block, and the block would be
        rendered from a payload that is out of date: the assignees, the tags and every mention
        revert, and for a closed issue nothing comes along afterwards to put them back. Being
        late about a lock is worth fixing. Being late about everything else is what the guard is
        for, and an earlier look proved what gets through it.
        """
        await issues.sync(issue_event("opened"))
        await issues.sync(
            issue_event(
                "closed",
                state="closed",
                closed_at="2026-08-11T12:00:00Z",
                updated_at="2026-08-11T12:00:00Z",
            )
        )
        thread = threads.created[0].thread_id
        block = threads.metadata_of(thread)

        # As an item closed before this column existed reads, and as one whose lock was refused
        # reads: the row does not say the thread is shut.
        item = await stored(db_session)
        await db_session.execute(
            update(TrackedItem).where(TrackedItem.id == item.id).values(discord_thread_locked=None)
        )
        await db_session.commit()
        threads.threads[thread].locked = False

        result = await issues.sync(issue_event("edited", labels=[{"name": "wontfix"}]))

        assert result.outcome is SyncOutcome.STALE, "the old payload was believed"
        assert threads.metadata_of(thread) == block, "an old payload rewrote the block"
        assert threads.threads[thread].locked is True, "the lock it was owed was dropped"

    async def test_a_thread_already_shut_costs_no_call(
        self,
        registered: Repository,
        issues: ItemSyncService,
        threads: FakeThreadGateway,
        issue_event,
    ) -> None:
        """The ordinary stale delivery, which is most of them. The row says the thread is shut,
        so there is nothing owed and nothing to ask Discord.

        Asserted on what Discord was asked rather than on where the lock ended up. Setting a lock
        to what it already is moves nothing, so a test written against `locks` here passes
        whether the call was made or not, which is what this one did when it was written.
        """
        await issues.sync(issue_event("opened"))
        await issues.sync(
            issue_event(
                "closed",
                state="closed",
                closed_at="2026-08-11T12:00:00Z",
                updated_at="2026-08-11T12:00:00Z",
            )
        )
        settled = len(threads.lock_calls)

        await issues.sync(issue_event("edited"))

        assert threads.lock_calls[settled:] == [], "it asked Discord about a lock already recorded"

    async def test_an_item_nobody_has_finished_costs_no_call(
        self,
        registered: Repository,
        issues: ItemSyncService,
        threads: FakeThreadGateway,
        issue_event,
    ) -> None:
        """An open issue's thread is one people are meant to be talking in, so a stale delivery
        for one is owed nothing at all."""
        await issues.sync(issue_event("opened", updated_at="2026-08-11T12:00:00Z"))
        opened = len(threads.locks)

        await issues.sync(issue_event("edited", updated_at="2026-08-11T09:00:00Z"))

        assert threads.locks[opened:] == [], "it went looking for a lock on an open issue"


class TestTheLockSurvivingAnOrdinaryDelivery:
    """The single line the thread lock rests on, and nothing was watching it.

    The write path swaps a thread for itself after every update, to put the metadata message id
    back where Discord has moved it. That swap goes through the same conditional write as a
    genuine replacement, so clearing the lock on it wiped the column on every ordinary delivery:
    the sync asked Discord to shut a thread it had already shut, and the staleness guard stood
    open for every finished item. Three reviewers caught it by eye and the suite did not notice,
    which is why this is here.
    """

    async def test_the_column_is_not_cleared_by_a_thread_swapped_for_itself(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker,
        db_session: AsyncSession,
        threads: FakeThreadGateway,
        pr_event,
    ) -> None:
        """A pull request, deliberately. An issue's lock is asked for by its payload on every
        delivery, so it writes the column back down each time and the mistake cannot be seen in
        the row at all. A pull request's is asked for only when the row says it is owed, so a
        column that has been wiped shows up as a Discord call nobody needed.
        """
        prs = build_item_sync(db_sessionmaker, threads, PullRequestPolicy())
        await prs.sync(pr_event("opened"))

        # Finished, the way `/set_done` leaves it.
        item = await stored(db_session)
        await db_session.execute(
            update(TrackedItem).where(TrackedItem.id == item.id).values(status=Status.DONE)
        )
        await db_session.commit()

        await prs.sync(pr_event("labeled", updated_at="2026-08-11T10:30:00Z"))
        assert (await stored(db_session)).discord_thread_locked is True, "it never shut it"
        shut_it = len(threads.lock_calls)
        assert shut_it == 1, "it did not ask Discord to shut it, so this proves nothing"

        # Two more, because the row is read before the write that would have wiped it. Wiping it
        # shows up one delivery later, and then on every other one after that.
        await prs.sync(pr_event("edited", updated_at="2026-08-11T11:30:00Z"))
        await prs.sync(pr_event("edited", updated_at="2026-08-11T12:30:00Z"))

        assert threads.lock_calls[shut_it:] == [], (
            "an ordinary delivery forgot the thread was shut and asked Discord again"
        )


class _RefusesThePost(FakeThreadGateway):
    """Discord having the ordinary bad moment the whole retry mechanism exists for."""

    async def post(self, *, thread_id: int, content: str) -> int | None:
        raise DiscordGatewayError("Discord refused the post")


class TestAClaimThatCouldNotBeGivenBack:
    """The claim is taken before the post so a retry cannot put the same comment in twice. Which
    means a claim that is not given back is a comment that will never be posted: the retry reads
    it as already there, answers PROCESSED, and the queue clears the error with it.

    It needs the Discord post and the hand-back to fail together, which is rare. It used to be
    rare and silent.
    """

    async def test_it_says_the_comment_is_lost(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker,
        threads: FakeThreadGateway,
        db_session: AsyncSession,
        caplog: pytest.LogCaptureFixture,
        issue_event,
    ) -> None:
        issues = build_item_sync(db_sessionmaker, threads, IssuePolicy())
        await issues.sync(issue_event("opened"))

        mirror = ItemNoteMirror(
            db_sessionmaker, _RefusesThePost(), render=lambda note, mentions: "hello"
        )

        async def the_database_went_away(*args: object, **kwargs: object) -> None:
            raise RuntimeError("could not check out a connection")

        mirror._release = the_database_went_away

        with caplog.at_level(logging.ERROR), pytest.raises(DiscordGatewayError):
            await mirror.mirror(parse_comment_event("created", payloads.issue_comment_event()))

        assert "could not give back the claim" in caplog.text, "the comment was lost in silence"


class TestARebuildThatDidNotWorkTheFirstTime:
    """The note path's only way out of a deleted thread, and it used to get one try at it.

    Nothing else on this path can build a thread: a comment is not an item event, and only a
    sync has the channel and the metadata. So the ask is the whole recovery, and it was made
    from the branch that clears the dead pointer on its way past. That branch cannot be reached
    a second time. Every attempt after the first stopped one step earlier, at the item with no
    thread, where nothing asked for anything, and one 502 from GitHub or one delivery deadline
    landing inside the rebuild ended the item's mirror for good: sixteen attempts, every later
    comment the same, and no thread until an item event happened to arrive, which for a closed
    issue is never.
    """

    @pytest.fixture
    def issues(
        self, db_sessionmaker: async_sessionmaker, threads: FakeThreadGateway
    ) -> ItemSyncService:
        return build_item_sync(db_sessionmaker, threads, IssuePolicy())

    async def test_it_is_asked_for_again_and_the_note_lands(
        self,
        registered: Repository,
        issues: ItemSyncService,
        db_sessionmaker: async_sessionmaker,
        threads: FakeThreadGateway,
        issue_event,
    ) -> None:
        asks: list[int] = []

        async def rebuild(note) -> None:
            asks.append(note.item_number)
            if len(asks) == 1:
                raise RuntimeError("GitHub answered 502")
            await issues.sync(issue_event("edited"))

        mirror = ItemNoteMirror(
            db_sessionmaker, threads, render=lambda note, mentions: "hello", rebuild=rebuild
        )
        synced = await issues.sync(issue_event("opened"))
        threads.threads.pop(synced.thread_id)
        note = parse_comment_event("created", payloads.issue_comment_event())

        # The attempt that finds the thread gone, asks, and is refused.
        with pytest.raises(ItemNotReadyError):
            await mirror.mirror(note)
        # The retry, which finds an item with no thread at all.
        with pytest.raises(ItemNotReadyError):
            await mirror.mirror(note)
        posted = await mirror.mirror(note)

        assert asks == [note.item_number] * 2, "it only ever asked once"
        assert posted is True
        assert threads.posts == [(threads.created[-1].thread_id, "hello")]

    async def test_the_failure_is_kept_out_of_the_reason_the_note_gives(
        self,
        registered: Repository,
        issues: ItemSyncService,
        db_sessionmaker: async_sessionmaker,
        threads: FakeThreadGateway,
        issue_event,
    ) -> None:
        """What the delivery is told has to name the thread, not whatever GitHub said.

        The note is retried either way, and a rebuild that cannot happen now may work on the
        attempt after, so the rebuild failing is not the note's reason for stopping.
        """

        async def rebuild(note) -> None:
            raise RuntimeError("GitHub answered 502")

        mirror = ItemNoteMirror(
            db_sessionmaker, threads, render=lambda note, mentions: "hello", rebuild=rebuild
        )
        synced = await issues.sync(issue_event("opened"))
        threads.threads.pop(synced.thread_id)

        with pytest.raises(ItemNotReadyError, match=str(synced.thread_id)):
            await mirror.mirror(parse_comment_event("created", payloads.issue_comment_event()))


class TestTheSlotBeingClearedMidRebuild:
    """The branch that guards the narrowest race here, and nothing exercised it.

    A rebuild swaps from the dead id it started with. If the note mirror lets go of that same
    dead thread while the rebuild is in flight, the swap matches nothing, and the replacement
    would be deleted with the item left holding no thread at all.
    """

    async def test_the_replacement_is_kept_when_the_slot_empties_underneath(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker,
        db_session: AsyncSession,
        pr_event,
    ) -> None:
        threads = _ClearsTheSlotWhileCreating(db_sessionmaker)
        service = build_item_sync(db_sessionmaker, threads, PullRequestPolicy())
        first = await service.sync(pr_event("opened"))
        threads.threads.pop(first.thread_id)
        threads.arm(first.tracked_item_id, first.thread_id)

        second = await service.sync(pr_event("edited", title="Rebuilt"))

        assert second.thread_id is not None
        assert second.thread_id in threads.threads
        assert (await stored(db_session)).discord_thread_id == second.thread_id
        assert threads.deleted == [], "the replacement was thrown away"

    async def test_an_item_that_has_gone_takes_its_thread_with_it(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker,
        pr_event,
    ) -> None:
        """Unregistering mid-flight leaves nothing to attach to, and a thread nobody can reach."""
        threads = _DeletesTheItemWhileCreating(db_sessionmaker)
        service = build_item_sync(db_sessionmaker, threads, PullRequestPolicy())

        with pytest.raises(ItemNotReadyError, match="no longer there"):
            await service.sync(pr_event("opened"))

        assert len(threads.deleted) == 1
        assert threads.threads == {}


class _ClearsTheSlotWhileCreating(FakeThreadGateway):
    """Lets go of the dead thread at the moment the replacement is being opened."""

    def __init__(self, sessionmaker) -> None:
        super().__init__()
        self._sessionmaker = sessionmaker
        self._item: int | None = None
        self._dead: int | None = None

    def arm(self, tracked_item_id: int, dead_thread_id: int) -> None:
        self._item, self._dead = tracked_item_id, dead_thread_id

    async def create(self, *, channel_id: int, name: str, content: str) -> ThreadHandle:
        if self._item is not None:
            async with self._sessionmaker() as session, session.begin():
                await ThreadPointerStore(session).forget_thread(
                    self._item, dead_thread_id=self._dead
                )
            self._item = None
        return await super().create(channel_id=channel_id, name=name, content=content)


class _DeletesTheItemWhileCreating(FakeThreadGateway):
    """Removes the tracked item itself while the thread is being opened."""

    def __init__(self, sessionmaker) -> None:
        super().__init__()
        self._sessionmaker = sessionmaker

    async def create(self, *, channel_id: int, name: str, content: str) -> ThreadHandle:
        handle = await super().create(channel_id=channel_id, name=name, content=content)
        async with self._sessionmaker() as session, session.begin():
            await session.execute(delete(TrackedItem))
        return handle


class TestTheContainerLettingGoOnDiscordSaySo:
    """What the gateway listener calls, wired to the rows.

    Discord reports a deleted thread on the gateway whether or not this bot cared about it, so
    the common case is a thread belonging to nobody here.
    """

    async def test_the_item_pointing_at_it_lets_go(
        self, registered: Repository, db_engine: AsyncEngine, db_session: AsyncSession, issue_event
    ) -> None:
        container = build_stack(db_engine, threads=FakeThreadGateway())
        synced = await container.issue_sync.sync(issue_event("opened"))

        await container.forget_thread(synced.thread_id)

        assert (await stored(db_session)).discord_thread_id is None

    async def test_a_channel_going_lets_go_of_everything_that_was_in_it(
        self, registered: Repository, db_engine: AsyncEngine, db_session: AsyncSession, issue_event
    ) -> None:
        """The half the per-thread event cannot cover, because Discord reports a thread deleted
        with its channel only while discord.py still has that thread cached."""
        container = build_stack(db_engine, threads=FakeThreadGateway())
        await container.issue_sync.sync(issue_event("opened"))
        item = await stored(db_session)

        await container.forget_channel(item.discord_channel_id)

        db_session.expire_all()
        assert (await stored(db_session)).discord_thread_id is None

    async def test_a_channel_holding_nothing_here_is_left_alone(
        self, registered: Repository, db_engine: AsyncEngine, db_session: AsyncSession, issue_event
    ) -> None:
        """Which is most of the channels a server ever deletes."""
        container = build_stack(db_engine, threads=FakeThreadGateway())
        synced = await container.issue_sync.sync(issue_event("opened"))

        await container.forget_channel(999_999)

        assert (await stored(db_session)).discord_thread_id == synced.thread_id

    async def test_a_thread_nobody_here_owns_is_left_alone(
        self, registered: Repository, db_engine: AsyncEngine, db_session: AsyncSession, issue_event
    ) -> None:
        container = build_stack(db_engine, threads=FakeThreadGateway())
        synced = await container.issue_sync.sync(issue_event("opened"))

        await container.forget_thread(999_999)

        assert (await stored(db_session)).discord_thread_id == synced.thread_id

    async def test_a_pointer_that_has_moved_on_is_left_alone(
        self, registered: Repository, db_engine: AsyncEngine, db_session: AsyncSession, issue_event
    ) -> None:
        """A delete reported for a thread the item has already replaced must not strand the
        replacement, which is the same guard the note mirror needed."""
        threads = FakeThreadGateway()
        container = build_stack(db_engine, threads=threads)
        first = await container.issue_sync.sync(issue_event("opened"))
        threads.threads.pop(first.thread_id)
        rebuilt = await container.issue_sync.sync(issue_event("edited"))

        await container.forget_thread(first.thread_id)

        assert (await stored(db_session)).discord_thread_id == rebuilt.thread_id
