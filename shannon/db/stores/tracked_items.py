from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shannon.db.models import TrackedItem
from shannon.domain.enums import ObjectType, Priority, Status
from shannon.domain.time import as_utc


class TrackedItemStore:
    """Data access for synced GitHub objects."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(
        self, *, repository_id: int, object_type: ObjectType, github_object_id: int
    ) -> TrackedItem | None:
        return await self._session.scalar(
            select(TrackedItem).where(
                TrackedItem.repository_id == repository_id,
                TrackedItem.github_object_type == object_type,
                TrackedItem.github_object_id == github_object_id,
            )
        )

    async def get_by_id(self, tracked_item_id: int) -> TrackedItem | None:
        return await self._session.get(TrackedItem, tracked_item_id)

    def raise_updated_at(self, item: TrackedItem, incoming: datetime) -> None:
        """Move the item's high-water mark up, and never down.

        Two syncs of one item overlap by design, so a comparison in Python decides against a
        value already stale by commit time. GREATEST compares against the row as it stands at
        write time. Nulls are ignored, covering an item that has never carried a timestamp.

        Lowering the mark blinds the staleness guard to the next delivery too, and the lock step
        reads this same field to decide whether it has been superseded.

        Staged, not executed: it flushes with the item's other changes in one statement.
        """
        item.github_updated_at = func.greatest(TrackedItem.github_updated_at, as_utc(incoming))

    async def get_by_number(
        self,
        *,
        repository_id: int,
        number: int,
        object_type: ObjectType | None = None,
    ) -> TrackedItem | None:
        """Find an item by its GitHub number.

        This exists because `issue_comment` payloads report the issue id even for a pull
        request, and that never matches the pull request id stored against the tracked item.
        The number matches for both.

        `object_type` narrows it further. Issues and pull requests share one numbering sequence
        per repository, so a number already identifies exactly one of them, but pinning the
        type means a caller that knows what it is cannot be handed the other.
        """
        query = select(TrackedItem).where(
            TrackedItem.repository_id == repository_id,
            TrackedItem.github_object_number == number,
        )
        if object_type is not None:
            query = query.where(TrackedItem.github_object_type == object_type)
        return await self._session.scalar(query.order_by(TrackedItem.id))

    async def get_or_create(
        self,
        *,
        repository_id: int,
        object_type: ObjectType,
        github_object_id: int,
        github_object_number: int,
        github_url: str,
        title: str,
        github_state: str,
        status: Status,
        priority: Priority = Priority.UNSET,
        github_updated_at: datetime | None = None,
    ) -> TrackedItem:
        """Insert the item, or return the one another delivery inserted first.

        GitHub fires several events at once for a newly opened item, and they carry different
        delivery ids so the duplicate guard passes all of them through. Checking for the row
        and then inserting it would let every one of them past the check and leave all but one
        failing on the unique constraint, so the insert carries its own conflict handling.
        """
        statement = (
            pg_insert(TrackedItem)
            .values(
                repository_id=repository_id,
                github_object_type=object_type,
                github_object_id=github_object_id,
                github_object_number=github_object_number,
                github_url=github_url,
                title=title,
                github_state=github_state,
                status=status,
                priority=priority,
                github_updated_at=github_updated_at,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    TrackedItem.repository_id,
                    TrackedItem.github_object_type,
                    TrackedItem.github_object_id,
                ]
            )
            .returning(TrackedItem.id)
        )
        inserted = (await self._session.execute(statement)).scalar_one_or_none()

        item = (
            await self.get_by_id(inserted)
            if inserted is not None
            else await self.get(
                repository_id=repository_id,
                object_type=object_type,
                github_object_id=github_object_id,
            )
        )
        if item is None:
            raise RuntimeError(
                f"tracked item for {object_type.value} {github_object_id} vanished mid-write"
            )
        return item

    async def set_discord_ids(
        self, item: TrackedItem, *, thread_id: int | None, message_id: int | None
    ) -> None:
        item.discord_thread_id = thread_id
        item.discord_message_id = message_id
        await self._session.flush()

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
            .values(discord_thread_id=None, discord_message_id=None)
            .execution_options(synchronize_session=False)
        )
        return bool(result.rowcount)

    async def claim_thread(
        self,
        tracked_item_id: int,
        *,
        thread_id: int,
        message_id: int | None,
        replacing: int | None,
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
        await self._session.execute(
            update(TrackedItem)
            .where(
                TrackedItem.id == tracked_item_id,
                TrackedItem.discord_thread_id.is_not_distinct_from(replacing),
            )
            .values(discord_thread_id=thread_id, discord_message_id=message_id)
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
