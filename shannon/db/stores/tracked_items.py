from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
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

    async def get_by_thread(self, discord_thread_id: int) -> TrackedItem | None:
        """Find the item a Discord thread belongs to.

        The workflow commands take no argument and act on the thread they are run in, because
        the thread is the only thing the person running one is looking at. Nothing else looks
        an item up this way, which is why the column carries no index: one row per thread and a
        handful of commands a day is not a query worth an index to maintain on the sync path.
        """
        return await self._session.scalar(
            select(TrackedItem).where(TrackedItem.discord_thread_id == discord_thread_id)
        )

    async def get_by_number(
        self, *, repository_id: int, number: int, object_type: ObjectType
    ) -> TrackedItem | None:
        """Find an item by its GitHub number and kind.

        By number because `issue_comment` payloads report the issue id even for a pull request,
        and that never matches the pull request id stored against the tracked item. The number
        matches for both.

        The kind is required rather than optional. Issues and pull requests share one numbering
        sequence per repository, so a number already identifies exactly one of them, and every
        caller knows which it is asking about; leaving it out would only be a way to be handed
        the other one.
        """
        return await self._session.scalar(
            select(TrackedItem)
            .where(
                TrackedItem.repository_id == repository_id,
                TrackedItem.github_object_number == number,
                TrackedItem.github_object_type == object_type,
            )
            .order_by(TrackedItem.id)
        )

    async def mirrored_state(
        self, *, repository_id: int, object_type: ObjectType
    ) -> dict[int, tuple[datetime | None, int | None]]:
        """When each item of one kind was last seen, and whether it reached Discord.

        For the poller, which is handed a whole board every minute and has to work out which of
        it moved. Three columns rather than whole rows, and one query rather than one per card:
        the answer is compared and thrown away, and a board can be long.

        The thread is here because the timestamp alone is not enough to decide. A row is written
        and committed before the Discord call that gives it a thread, so an item can be recorded
        as current while nobody can see it, and a poller comparing only timestamps would never
        look at it again.
        """
        rows = await self._session.execute(
            select(
                TrackedItem.github_object_id,
                TrackedItem.github_updated_at,
                TrackedItem.discord_thread_id,
            ).where(
                TrackedItem.repository_id == repository_id,
                TrackedItem.github_object_type == object_type,
            )
        )
        return {row[0]: (row[1], row[2]) for row in rows.all()}

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
