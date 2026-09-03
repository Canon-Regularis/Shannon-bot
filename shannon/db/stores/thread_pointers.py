"""Which Discord thread a tracked item points at.

Apart from the rest of the item's data because these two writes are the only ones in the schema
that are conditional on what the row already says. Both swap from the thread id the caller last
saw, which is what stops two syncs of one item attaching two threads, and what stops a note
mirror clearing a pointer another sync has since replaced.

Kept together and kept small so that adding an unconditional write here looks as wrong as it is.
One was added once, as `set_discord_ids`, and sat unused beside the guarded pair for long enough
to be worth designing against.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from shannon.db.models import TrackedItem


class ThreadPointerStore:
    """The item's thread and message ids, written only against what they currently are."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def forget_thread(self, tracked_item_id: int, *, dead_thread_id: int) -> bool:
        """Drop a thread pointer, unless the item has already moved on to a different thread.

        Reported by whoever found the thread gone, which may be a step behind: another sync can
        have rebuilt it in the meantime, and clearing the pointer then would strand the new
        thread exactly as the old one was stranded.
        """
        result = await self._session.execute(
            update(TrackedItem)
            .where(
                TrackedItem.id == tracked_item_id,
                TrackedItem.discord_thread_id == dead_thread_id,
            )
            # The lock goes with the pointer. Whatever this bot had made the dead thread, the
            # replacement starts open.
            .values(
                discord_thread_id=None,
                discord_message_id=None,
                discord_thread_locked=None,
                discord_channel_id=None,
            )
            .execution_options(synchronize_session=False)
        )
        return bool(result.rowcount)

    async def forget_channel(self, channel_id: int) -> Sequence[int]:
        """Let go of every thread that was in a channel, because the channel has gone.

        Discord deletes the threads with it and reports each one, but only while discord.py still
        has that thread cached, and it drops one the moment the thread archives. So the live
        threads announce themselves and the quiet ones do not, and the quiet ones are the whole
        reason any of this exists: a draft card parked in a column nobody touches has no webhook
        to rebuild it and no visitor but the poller, which decides from a stored pointer without
        asking Discord.

        Matched on where the thread actually is rather than on where the mapping says new threads
        go. Those are different questions the moment anybody runs `/set_channel`, which moves the
        second and leaves the first alone, and answering with the mapping would let go of threads
        that are alive in the previous channel.

        A row written before the channel was recorded has none, and is left alone: it is not
        known to have been in there, and letting go of a thread that is fine opens a second one
        beside it. Those rows are covered from the first time their thread is rebuilt.

        Answers with the items it let go of, for the caller to say so.
        """
        found = (
            await self._session.scalars(
                update(TrackedItem)
                .where(TrackedItem.discord_channel_id == channel_id)
                .values(
                    discord_thread_id=None,
                    discord_message_id=None,
                    discord_thread_locked=None,
                    discord_channel_id=None,
                )
                .returning(TrackedItem.id)
                .execution_options(synchronize_session=False)
            )
        ).all()
        return list(found)

    async def note_the_lock(self, tracked_item_id: int, *, thread_id: int, locked: bool) -> None:
        """Record what this bot has just made the lock on a thread.

        Against the thread it was made on, like everything else here, so a sync that locked a
        thread another sync has since replaced does not describe the replacement. That one is
        open, and the row saying otherwise would leave a finished item with a thread anybody can
        post in and nothing left to notice it.
        """
        await self._session.execute(
            update(TrackedItem)
            .where(
                TrackedItem.id == tracked_item_id,
                TrackedItem.discord_thread_id == thread_id,
            )
            .values(discord_thread_locked=locked)
            .execution_options(synchronize_session=False)
        )

    async def claim_thread(
        self,
        tracked_item_id: int,
        *,
        thread_id: int,
        message_id: int | None,
        replacing: int | None,
        channel_id: int | None = None,
    ) -> tuple[int | None, int | None]:
        """Point an item at a thread, but only if it still points where the caller thinks.

        Returns the ids the item ended up with, which are the caller's own only if it won.

        The Discord round trip that creates a thread happens outside any transaction, so two
        callers can both read the same starting state and both create one: the worker and `/pr`
        race whenever somebody runs the command while an event for the same item is in flight.
        Swapping from the exact id that was read, rather than writing unconditionally, is what
        keeps an item pointing at one thread. `replacing` is None on first creation and the id
        of the dead thread when rebuilding, and `IS NOT DISTINCT FROM` makes those one case.
        """
        moving: dict[str, object] = {
            "discord_thread_id": thread_id,
            "discord_message_id": message_id,
        }
        if channel_id is not None:
            # Where the thread actually is, as opposed to where the mapping currently says new
            # ones should go. A channel deletion has nothing else to go on.
            moving["discord_channel_id"] = channel_id
        if replacing != thread_id:
            # The item is being pointed at a different thread, so whatever this bot had made the
            # old one says nothing about the new one, which starts open.
            #
            # Only when it is a different thread. The write path swaps a thread for itself after
            # every ordinary update, to put the metadata message id back when Discord moved it,
            # and that is not a new thread. Clearing on those said every thread was freshly
            # opened, which is the state that means the lock has not been settled: the sync asked
            # Discord to shut a thread it had already shut on every delivery, and the staleness
            # guard let every superseded delivery for a finished item straight through.
            moving["discord_thread_locked"] = None

        await self._session.execute(
            update(TrackedItem)
            .where(
                TrackedItem.id == tracked_item_id,
                TrackedItem.discord_thread_id.is_not_distinct_from(replacing),
            )
            .values(**moving)
            .execution_options(synchronize_session=False)
        )
        row = (
            await self._session.execute(
                select(TrackedItem.discord_thread_id, TrackedItem.discord_message_id).where(
                    TrackedItem.id == tracked_item_id
                )
            )
        ).one_or_none()
        return (row[0], row[1]) if row is not None else (None, None)
