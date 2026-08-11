from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shannon.db.models import ChannelMapping
from shannon.domain.enums import ObjectType


class ChannelMappingStore:
    """Data access for the channel each object type is posted into."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # Where a type falls back to when nothing has been mapped for it. /register only ever maps
    # pull requests, so without this an issue would have nowhere to go until someone ran
    # /set_channel, and nothing would appear to be happening.
    FALLBACK = ObjectType.PR

    async def get(self, repository_id: int, object_type: ObjectType) -> ChannelMapping | None:
        return await self._session.scalar(
            select(ChannelMapping).where(
                ChannelMapping.repository_id == repository_id,
                ChannelMapping.object_type == object_type,
            )
        )

    async def resolve(self, repository_id: int, object_type: ObjectType) -> ChannelMapping | None:
        """The channel to post this kind of item into, falling back where nothing is mapped.

        Distinct from `get`, which answers only what was actually configured. `/set_channel`
        needs the literal answer; the sync path wants somewhere to post.
        """
        mapping = await self.get(repository_id, object_type)
        if mapping is not None or object_type is self.FALLBACK:
            return mapping
        return await self.get(repository_id, self.FALLBACK)

    async def set(
        self, *, repository_id: int, object_type: ObjectType, discord_channel_id: int
    ) -> ChannelMapping:
        """Point a type at a channel, whether or not one was mapped before.

        The insert settles the conflict itself. Reading first and then writing lets two people
        running /set_channel at once both find nothing and both insert, and the second one hits
        the unique constraint.
        """
        statement = (
            pg_insert(ChannelMapping)
            .values(
                repository_id=repository_id,
                object_type=object_type,
                discord_channel_id=discord_channel_id,
            )
            .on_conflict_do_update(
                constraint="uq_channel_mappings_repo_type",
                # updated_at is set here because onupdate only fires for an ORM update, and this
                # never goes through one.
                set_={"discord_channel_id": discord_channel_id, "updated_at": func.now()},
            )
            .returning(ChannelMapping)
        )
        return (await self._session.scalars(statement)).one()
