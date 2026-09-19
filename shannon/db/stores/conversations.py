"""Which threads are being published to GitHub, and which batch is in flight. Issue #103."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shannon.db.models import LoggedConversation, LoggedMessage


@dataclass(frozen=True, slots=True)
class PendingBatch:
    """One conversation with something waiting, and everything the flush decision needs.

    Read in one grouped query rather than a row per conversation followed by a count each,
    because the tick runs every few seconds and most ticks find nothing to do.
    """

    conversation_id: int
    tracked_item_id: int
    discord_thread_id: int
    count: int
    characters: int
    oldest: datetime
    newest: datetime
    through_id: int
    stopped: bool
    flush_id: str | None
    flush_started_at: datetime | None
    flush_through_id: int | None
    failed_flushes: int


class ConversationStore:
    """The conversations this bot is capturing, and the claim on the batch each is publishing."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def start(
        self, *, tracked_item_id: int, discord_thread_id: int, started_by: int, now: datetime
    ) -> int | None:
        """Begin logging this item, reporting the new row's id, or None if one is already open.

        The insert settles that rather than a read first, because the partial unique index is
        already the rule and asking twice would let two commands run together both find nothing.
        """
        found: int | None = await self._session.scalar(
            pg_insert(LoggedConversation)
            .values(
                tracked_item_id=tracked_item_id,
                discord_thread_id=discord_thread_id,
                started_by_discord_user_id=started_by,
                started_at=now,
            )
            .on_conflict_do_nothing(
                index_elements=[LoggedConversation.tracked_item_id],
                index_where=text("stopped_at IS NULL"),
            )
            .returning(LoggedConversation.id)
        )
        return found

    async def stop(self, *, tracked_item_id: int, stopped_by: int, now: datetime) -> int | None:
        """End logging, reporting the thread that was being captured, or None if none was.

        What is pending is deliberately left alone. The flusher reads a stopped conversation as a
        reason to publish at once, so ending the logging is what gets the tail of it onto GitHub
        rather than what throws it away.
        """
        found: int | None = await self._session.scalar(
            update(LoggedConversation)
            .where(
                LoggedConversation.tracked_item_id == tracked_item_id,
                LoggedConversation.stopped_at.is_(None),
            )
            .values(stopped_at=now, stopped_by_discord_user_id=stopped_by)
            .returning(LoggedConversation.discord_thread_id)
        )
        return found

    async def open_for_thread(self, discord_thread_id: int) -> int | None:
        """The open conversation capturing this thread, if there is one."""
        found: int | None = await self._session.scalar(
            select(LoggedConversation.id).where(
                LoggedConversation.discord_thread_id == discord_thread_id,
                LoggedConversation.stopped_at.is_(None),
            )
        )
        return found

    async def live_threads(self) -> Sequence[int]:
        """Every thread currently being captured, for a process that has just started.

        What the in-memory set is filled from. Without it a restart would leave every conversation
        armed in the database and captured by nothing, silently.
        """
        found = await self._session.scalars(
            select(LoggedConversation.discord_thread_id).where(
                LoggedConversation.stopped_at.is_(None)
            )
        )
        return list(found)

    async def stop_for_threads(self, thread_ids: Sequence[int], *, now: datetime) -> None:
        """End logging for threads that have gone, so nothing is left armed against a dead one."""
        if not thread_ids:
            return
        await self._session.execute(
            update(LoggedConversation)
            .where(
                LoggedConversation.discord_thread_id.in_(thread_ids),
                LoggedConversation.stopped_at.is_(None),
            )
            .values(stopped_at=now)
        )

    async def stop_for_items(
        self, tracked_item_ids: Sequence[int], *, now: datetime
    ) -> Sequence[int]:
        """The same, for items that let go of their threads when a whole channel was deleted.

        By item rather than by thread because that is what the caller has: deleting a channel
        deletes every thread in it, and the store that clears the pointers answers with the items
        it cleared. The threads it stopped come back, so the in-memory set can drop them too.
        """
        if not tracked_item_ids:
            return ()
        stopped = await self._session.scalars(
            update(LoggedConversation)
            .where(
                LoggedConversation.tracked_item_id.in_(tracked_item_ids),
                LoggedConversation.stopped_at.is_(None),
            )
            .values(stopped_at=now)
            .returning(LoggedConversation.discord_thread_id)
        )
        return list(stopped)

    async def pending(self) -> Sequence[PendingBatch]:
        """Every conversation with messages waiting, and what the flush decision asks about it.

        An inner join, so a conversation with nothing pending does not appear at all. That is what
        makes a conversation stopped and already published disappear from the tick rather than
        being looked at for ever.
        """
        rows = await self._session.execute(
            select(
                LoggedConversation.id,
                LoggedConversation.tracked_item_id,
                LoggedConversation.discord_thread_id,
                func.count(LoggedMessage.id),
                func.coalesce(func.sum(func.length(LoggedMessage.content)), 0),
                func.min(LoggedMessage.said_at),
                func.max(LoggedMessage.said_at),
                func.max(LoggedMessage.id),
                LoggedConversation.stopped_at,
                LoggedConversation.flush_id,
                LoggedConversation.flush_started_at,
                LoggedConversation.flush_through_id,
                LoggedConversation.failed_flushes,
            )
            .join(LoggedMessage, LoggedMessage.conversation_id == LoggedConversation.id)
            .group_by(LoggedConversation.id)
            .order_by(LoggedConversation.id)
        )
        return [
            PendingBatch(
                conversation_id=row[0],
                tracked_item_id=row[1],
                discord_thread_id=row[2],
                count=row[3],
                characters=row[4],
                oldest=row[5],
                newest=row[6],
                through_id=row[7],
                stopped=row[8] is not None,
                flush_id=row[9],
                flush_started_at=row[10],
                flush_through_id=row[11],
                failed_flushes=row[12],
            )
            for row in rows.all()
        ]

    async def claim(
        self, conversation_id: int, *, flush_id: str, through_id: int, now: datetime
    ) -> bool:
        """Take the batch up to `through_id`, reporting whether it was ours to take.

        Guarded on there being no claim already, so a tick overlapping another cannot take a batch
        that is in flight. False means somebody else has it.
        """
        claimed = await self._session.scalar(
            update(LoggedConversation)
            .where(
                LoggedConversation.id == conversation_id,
                LoggedConversation.flush_id.is_(None),
            )
            .values(flush_id=flush_id, flush_started_at=now, flush_through_id=through_id)
            .returning(LoggedConversation.id)
        )
        return claimed is not None

    async def seize(self, conversation_id: int, *, flush_id: str, held: str, now: datetime) -> bool:
        """Take over a claim left behind by a process that died holding it.

        Guarded on the abandoned claim still being the one that was read, so two ticks cannot both
        decide it is stale and both republish. `flush_through_id` is left exactly as it was: the
        batch to finish is the one that was claimed, not whatever has arrived since.
        """
        seized = await self._session.scalar(
            update(LoggedConversation)
            .where(
                LoggedConversation.id == conversation_id,
                LoggedConversation.flush_id == held,
            )
            .values(flush_id=flush_id, flush_started_at=now)
            .returning(LoggedConversation.id)
        )
        return seized is not None

    async def release(self, conversation_id: int) -> None:
        """Let go of the claim, and forget any failures behind it.

        Run when the batch is done with, whether it was published or given up on. Clearing the
        count means an outage costs a conversation nothing once it is over.
        """
        await self._session.execute(
            update(LoggedConversation)
            .where(LoggedConversation.id == conversation_id)
            .values(flush_id=None, flush_started_at=None, flush_through_id=None, failed_flushes=0)
        )

    async def release_empty_claims(self) -> None:
        """Let go of a claim on a batch that no longer has anything in it.

        Reached one way only, and it is narrow enough to be worth saying: a batch is claimed, the
        process holding it dies, and everything in that batch is then deleted in Discord before
        anybody picks the claim up. The conversation has a claim and no rows, so `pending` skips
        it entirely, and without this the claim stands until the next message arrives and then
        costs that message the whole retry window.

        Safe against a batch actually in flight. Rows are deleted only in the same transaction
        that releases the claim, so a conversation being published always still has its rows.
        """
        await self._session.execute(
            update(LoggedConversation)
            .where(
                LoggedConversation.flush_id.is_not(None),
                ~select(LoggedMessage.id)
                .where(LoggedMessage.conversation_id == LoggedConversation.id)
                .exists(),
            )
            .values(flush_id=None, flush_started_at=None, flush_through_id=None)
        )

    async def note_failure(self, conversation_id: int) -> int:
        """Count a flush that did not land, answering how many in a row that now is.

        The claim is deliberately left standing. Releasing it would have the next tick, a few
        seconds later, try the same batch again, which during an outage is a write a second at
        GitHub. Held, the batch is retried once the claim reads as abandoned, which is the same
        path a process that died holding one takes.
        """
        failures = await self._session.scalar(
            update(LoggedConversation)
            .where(LoggedConversation.id == conversation_id)
            .values(failed_flushes=LoggedConversation.failed_flushes + 1)
            .returning(LoggedConversation.failed_flushes)
        )
        return failures or 0
