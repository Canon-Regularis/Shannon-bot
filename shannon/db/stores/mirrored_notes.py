from __future__ import annotations

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shannon.db.models import MirroredNote


class MirroredNoteStore:
    """Which notes have already been posted into an item's thread."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim(self, tracked_item_id: int, note_key: str) -> bool:
        """Take responsibility for posting this note, reporting whether it was ours to take.

        False means somebody already has it, which on a retried delivery means it is already in
        the thread. The insert settles that itself rather than reading first: two workers can
        lease the same row once a lease has expired, and a read followed by a write would have
        both find nothing and both post.
        """
        claimed = await self._session.scalar(
            pg_insert(MirroredNote)
            .values(tracked_item_id=tracked_item_id, note_key=note_key)
            .on_conflict_do_nothing(constraint="uq_mirrored_notes_item_note")
            .returning(MirroredNote.id)
        )
        return claimed is not None

    async def release(self, tracked_item_id: int, note_key: str) -> None:
        """Hand a claim back, for when the note did not reach the thread after all."""
        await self._session.execute(
            delete(MirroredNote).where(
                MirroredNote.tracked_item_id == tracked_item_id,
                MirroredNote.note_key == note_key,
            )
        )
