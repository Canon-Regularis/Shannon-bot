"""Which Discord thread a tracked item points at.

Every write here is conditional on the thread id the caller last saw. That is what stops two
syncs of one item attaching two threads, and what stops a note mirror clearing a pointer that
another sync has since replaced.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Final

from sqlalchemy import Text, Update, func, select, update
from sqlalchemy.dialects.postgresql import array
from sqlalchemy.ext.asyncio import AsyncSession

from shannon.db.base import rows_changed
from shannon.db.models import TrackedItem

# The coalesce default: a row that remembers nothing has shown no labels.
_NOTHING = array((), type_=Text())

# What belongs to one thread rather than to the item: a replacement thread starts open, and has
# shown its reader nothing until its own block lands. Letting go of a thread and moving to a
# different one both clear these, so a column added here is cleared by both (#268).
_A_THREADS_OWN: Final[Mapping[str, None]] = MappingProxyType(
    {"discord_thread_locked": None, "shown_labels": None}
)

# Letting go of a thread: the pointer, where it was, and everything that went with it. Read-only
# so no caller can change it for the others (#268).
_LET_GO: Final[Mapping[str, None]] = MappingProxyType(
    {
        "discord_thread_id": None,
        "discord_message_id": None,
        "discord_channel_id": None,
        **_A_THREADS_OWN,
    }
)


def _guarded(tracked_item_id: int, thread_id: int) -> Update:
    return (
        update(TrackedItem)
        .where(
            TrackedItem.id == tracked_item_id,
            TrackedItem.discord_thread_id == thread_id,
        )
        .execution_options(synchronize_session=False)
    )


class ThreadPointerStore:
    """The item's thread and message ids, written only against what they currently are."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def forget_thread(self, tracked_item_id: int, *, dead_thread_id: int) -> bool:
        """Drop a thread pointer, unless the item has already moved on to a different thread.

        The report may be a step behind: another sync can have rebuilt the thread, and clearing
        the pointer then would strand the new one.
        """
        changed = await rows_changed(
            self._session, _guarded(tracked_item_id, dead_thread_id).values(**_LET_GO)
        )
        return bool(changed)

    async def forget_channel(self, channel_id: int) -> Sequence[int]:
        """Let go of every thread that was in a channel, because the channel has gone.

        Discord reports each thread it deletes with the channel, but only while discord.py still
        has that thread cached, and it drops one the moment the thread archives. The quiet ones
        are never reported, and nothing but this sweep clears their pointers.

        A row that remembers no channel is left alone: letting go of a thread that is still alive
        opens a second one beside it.
        """
        found = (
            await self._session.scalars(
                update(TrackedItem)
                .where(TrackedItem.discord_channel_id == channel_id)
                .values(**_LET_GO)
                .returning(TrackedItem.id)
                .execution_options(synchronize_session=False)
            )
        ).all()
        return list(found)

    async def note_the_lock(self, tracked_item_id: int, *, thread_id: int, locked: bool) -> None:
        """Record what this bot has just made the lock on a thread."""
        await self._session.execute(
            _guarded(tracked_item_id, thread_id).values(discord_thread_locked=locked)
        )

    async def remember_channel(
        self, tracked_item_id: int, *, thread_id: int, channel_id: int
    ) -> None:
        """Record where a thread turned out to be, having asked Discord.

        Rows claimed before this column existed remember no channel, and the answer costs a
        Discord call, so it is written down the first time it is known.

        Only what Discord said may go here, never the mapping: the mapping answers where new
        threads go, and `/set_channel` moves it while leaving existing threads where they are,
        so writing it in would make a stranded thread look settled.
        """
        await self._session.execute(
            _guarded(tracked_item_id, thread_id).values(discord_channel_id=channel_id)
        )

    async def note_shown_labels(
        self, tracked_item_id: int, *, thread_id: int, shown: Sequence[str]
    ) -> None:
        """Record the label names a block that was POSTED put in front of a reader.

        Only a posted one. An edit is invisible from the channel, so recording a block rewritten
        by a command would silence the line that command's own webhook produces, which is the
        only thing anybody else sees.
        """
        await self._session.execute(
            _guarded(tracked_item_id, thread_id).values(shown_labels=list(shown))
        )

    async def note_label_announced(
        self, tracked_item_id: int, *, thread_id: int, name: str, on_it: bool
    ) -> None:
        """Keep the shown set in step with a tag line that was actually posted.

        One statement rather than a read and a write, so two labels moving at once cannot lose
        each other, and so a repeated name is idempotent: delivery is at-least-once.

        The remove runs whichever way the line went, which is what lets a label come off and go
        back on and be announced both times. It runs for a label that came off unsaid as well,
        in a thread that has tag lines off (issue #163): nothing is added for one going on, so
        nothing records a line that was never said.
        """
        without = func.array_remove(func.coalesce(TrackedItem.shown_labels, _NOTHING), name)
        await self._session.execute(
            _guarded(tracked_item_id, thread_id).values(
                shown_labels=func.array_append(without, name) if on_it else without
            )
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

        The Discord round trip that creates a thread happens outside any transaction, so the
        worker and `/pr` can both read the same starting state and both create one. `replacing`
        is None on first creation and the id of the dead thread when rebuilding, and
        `IS NOT DISTINCT FROM` makes those one case.
        """
        moving: dict[str, object] = {
            "discord_thread_id": thread_id,
            "discord_message_id": message_id,
        }
        if channel_id is not None:
            # Where the thread actually is; a channel deletion has nothing else to go on.
            moving["discord_channel_id"] = channel_id
        if replacing != thread_id:
            # Only when the thread is different. The write path swaps a thread for itself after
            # every ordinary update, to put back a metadata message id Discord moved, and
            # clearing the lock there re-shuts a thread already shut and lets every superseded
            # delivery for a finished item past the staleness guard. The block that names the new
            # thread's labels is posted a moment later and records them itself.
            #
            # Not the whole of `_LET_GO`: the pointer is being set, not cleared, and a channel the
            # caller did not name is kept rather than forgotten.
            moving.update(_A_THREADS_OWN)

        won = (
            await self._session.execute(
                update(TrackedItem)
                .where(
                    TrackedItem.id == tracked_item_id,
                    TrackedItem.discord_thread_id.is_not_distinct_from(replacing),
                )
                .values(**moving)
                .returning(TrackedItem.discord_thread_id, TrackedItem.discord_message_id)
                .execution_options(synchronize_session=False)
            )
        ).one_or_none()
        if won is not None:
            # What this UPDATE wrote, which a SELECT in this transaction would read back: the
            # row stays locked until commit. One statement on the hot path, where the write path
            # swaps a thread for itself after every delivery (#268).
            return (won[0], won[1])
        # Lost, or the item is gone: the row as the winner left it, or nothing.
        row = (
            await self._session.execute(
                select(TrackedItem.discord_thread_id, TrackedItem.discord_message_id).where(
                    TrackedItem.id == tracked_item_id
                )
            )
        ).one_or_none()
        return (row[0], row[1]) if row is not None else (None, None)
