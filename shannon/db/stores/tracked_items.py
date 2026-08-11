from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shannon.db.models import TrackedItem
from shannon.domain.enums import ObjectType, Priority, Status


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
