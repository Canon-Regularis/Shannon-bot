from __future__ import annotations

import logging
from collections.abc import Mapping
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

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        fallbacks: Mapping[ObjectType, ObjectType] | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        # Where each kind's threads go when nobody has mapped a channel for it, which is only
        # read to answer where the ones already open are. Handed in rather than known here: the
        # sync policies decide it, and two copies of a product rule is one too many.
        self._fallbacks = dict(fallbacks or {})

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
            fallback = self._fallbacks.get(object_type)
            if existing is None and fallback is not None:
                # Where this kind's threads have actually been going. `/register` maps pull
                # requests and nothing else, so on most servers the first `/set_channel issues`
                # finds no issue row at all, and answering from that row alone said nothing
                # about the issue threads sitting in the pull request channel. That clause is
                # the whole reason this field exists: without it an admin tidying a server goes
                # looking for threads that never went anywhere.
                existing = await mappings.get(repository.id, fallback)
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
