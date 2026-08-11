from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shannon.db.models import ChannelMapping
from shannon.domain.enums import ObjectType


class ChannelMappingStore:
    """Data access for the channel each object type is posted into."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, repository_id: int, object_type: ObjectType) -> ChannelMapping | None:
        return await self._session.scalar(
            select(ChannelMapping).where(
                ChannelMapping.repository_id == repository_id,
                ChannelMapping.object_type == object_type,
            )
        )

    async def set(
        self, *, repository_id: int, object_type: ObjectType, discord_channel_id: int
    ) -> ChannelMapping:
        mapping = await self.get(repository_id, object_type)
        if mapping is None:
            mapping = ChannelMapping(
                repository_id=repository_id,
                object_type=object_type,
                discord_channel_id=discord_channel_id,
            )
            self._session.add(mapping)
        else:
            mapping.discord_channel_id = discord_channel_id
        await self._session.flush()
        return mapping
