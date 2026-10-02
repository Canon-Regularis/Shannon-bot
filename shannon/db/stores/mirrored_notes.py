from __future__ import annotations

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shannon.db.models import MirroredNote


class MirroredNoteStore:
    """Which notes have already been posted into an item's thread."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim(self, tracked_item_id: int, note_key: str) -> bool:
        """Take responsibility for posting this note, reporting whether it was ours to take.

        False means somebody already has it; on a retried delivery the note is already in the
        thread. Two workers can lease the same row once a lease expires, so the insert settles it.
        """
        claimed = await self._session.scalar(
            pg_insert(MirroredNote)
            .values(tracked_item_id=tracked_item_id, note_key=note_key)
            .on_conflict_do_nothing(constraint="uq_mirrored_notes_item_note")
            .returning(MirroredNote.id)
        )
        return claimed is not None

    async def remember_the_message(
        self, tracked_item_id: int, note_key: str, *, message_id: int | None
    ) -> None:
        """Write down which Discord message the note was posted as.

        A second write rather than part of `claim`, because the claim goes in before the post and
        this is only knowable after it. Issue #165: without it an edit on GitHub has nothing to
        point at, and the thread goes on showing text that no longer exists.

        None is accepted and written as it is, rather than guarded against by the caller. The
        column is nullable to mean "nobody knows which message this is", and a gateway that posted
        without saying what it posted is exactly that - the same state as a note mirrored before
        this column existed, which is already handled. Writing it is also the only arm reachable
        by a test: every implementation of `post` answers with an id, so a caller that branched on
        the absence of one would carry a branch nothing could ever take.

        Guarded on the key rather than blind, so a claim that was handed back and taken again by
        another worker is not overwritten by the loser's message id.
        """
        await self._session.execute(
            update(MirroredNote)
            .where(
                MirroredNote.tracked_item_id == tracked_item_id,
                MirroredNote.note_key == note_key,
            )
            .values(discord_message_id=message_id)
        )

    async def message_for(self, tracked_item_id: int, note_key: str) -> int | None:
        """Which message holds this note, or None if there is no answer to that.

        None covers both "no row" and "a row from before the id was kept", and the caller treats
        them the same way: there is nothing to edit. Telling them apart would buy nothing, because
        neither can be repaired from here - the first is handled by the claim succeeding instead.
        """
        return await self._session.scalar(
            select(MirroredNote.discord_message_id).where(
                MirroredNote.tracked_item_id == tracked_item_id,
                MirroredNote.note_key == note_key,
            )
        )

    async def release(self, tracked_item_id: int, note_key: str) -> None:
        """Hand a claim back, for when the note did not reach the thread after all."""
        await self._session.execute(
            delete(MirroredNote).where(
                MirroredNote.tracked_item_id == tracked_item_id,
                MirroredNote.note_key == note_key,
            )
        )
