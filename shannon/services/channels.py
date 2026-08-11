from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import async_sessionmaker

from shannon.db.stores.channel_mappings import ChannelMappingStore
from shannon.db.stores.repositories import RepositoryStore
from shannon.domain.enums import ObjectType
from shannon.domain.errors import NotRegisteredError

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ChannelAssignment:
    repository_name: str
    object_type: ObjectType
    discord_channel_id: int
    replaced: int | None


class ChannelMappingService:
    """Points an object type at the Discord channel its threads belong in."""

    def __init__(self, sessionmaker: async_sessionmaker) -> None:
        self._sessionmaker = sessionmaker

    async def assign(
        self, *, guild_id: int, object_type: ObjectType, channel_id: int
    ) -> ChannelAssignment:
        """Bind one object type to one channel, replacing whatever it pointed at before.

        Raises NotRegisteredError when the guild has no repository, because a mapping without
        one has nothing to hang off.
        """
        async with self._sessionmaker() as session, session.begin():
            repository = await RepositoryStore(session).get_by_guild(guild_id)
            if repository is None:
                raise NotRegisteredError("This server has no repository yet. Run /register first.")

            mappings = ChannelMappingStore(session)
            existing = await mappings.get(repository.id, object_type)
            replaced = existing.discord_channel_id if existing else None

            await mappings.set(
                repository_id=repository.id,
                object_type=object_type,
                discord_channel_id=channel_id,
            )
            assignment = ChannelAssignment(
                repository_name=repository.repo_name,
                object_type=object_type,
                discord_channel_id=channel_id,
                replaced=replaced,
            )

        logger.info(
            "guild %s now posts %s threads in channel %s", guild_id, object_type, channel_id
        )
        return assignment
