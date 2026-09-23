from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.stores.channel_mappings import ChannelMappingStore
from shannon.db.stores.repositories import RepositoryStore
from shannon.domain.enums import ObjectType
from shannon.domain.errors import NotRegisteredError

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PinnedKind:
    """A kind that was borrowing this channel and has just been given it outright."""

    object_type: ObjectType
    discord_channel_id: int


@dataclass(frozen=True, slots=True)
class ChannelAssignment:
    repository_name: str
    object_type: ObjectType
    discord_channel_id: int
    replaced: int | None
    pinned: tuple[PinnedKind, ...]


class ChannelMappingService:
    """Points an object type at the Discord channel its threads belong in."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        fallbacks: Mapping[ObjectType, ObjectType],
    ) -> None:
        self._sessionmaker = sessionmaker
        # Which kinds go to another kind's channel when nobody has mapped one for them. Handed
        # in because the sync policies decide it. Required rather than defaulted: a service
        # built without it would quietly stop pinning, which is the whole of issue #134.
        self._fallbacks = dict(fallbacks)

    async def assign(
        self, *, guild_id: int, object_type: ObjectType, channel_id: int
    ) -> ChannelAssignment:
        """Bind one object type to one channel, replacing whatever it pointed at before."""
        async with self._sessionmaker() as session, session.begin():
            repository = await RepositoryStore(session).get_by_guild(guild_id)
            if repository is None:
                raise NotRegisteredError("This server has no repository yet. Run /register first.")

            mappings = ChannelMappingStore(session)
            existing = await mappings.get(repository.id, object_type)
            fallback = self._fallbacks.get(object_type)
            if existing is None and fallback is not None:
                # Where this kind's threads have actually been going. `/register` maps pull
                # requests and nothing else, so the first `/set_channel issues` finds no issue
                # row, and the reply would say nothing about the issue threads already open.
                existing = await mappings.get(repository.id, fallback)
            replaced = existing.discord_channel_id if existing else None

            # Before the row moves, and that ordering is the fix rather than an implementation
            # detail: a transaction sees its own writes, so pinning after the upsert below would
            # copy the channel being set instead of the one being left, which is issue #134
            # again with the threads and the mapping disagreeing.
            pinned = await self._pin_the_borrowers(mappings, repository.id, object_type)

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
                pinned=pinned,
            )

        logger.info(
            "guild %s now posts %s threads in channel %s", guild_id, object_type, channel_id
        )
        for kept in assignment.pinned:
            logger.info(
                "guild %s keeps %s threads in channel %s, which they had been borrowing",
                guild_id,
                kept.object_type,
                kept.discord_channel_id,
            )
        return assignment

    async def _pin_the_borrowers(
        self, mappings: ChannelMappingStore, repository_id: int, object_type: ObjectType
    ) -> tuple[PinnedKind, ...]:
        """Give every kind borrowing this channel a row of its own, at the channel it is in now.

        So that pointing pull requests somewhere new stops meaning "and issues too". Unconditional
        even when the channel is unchanged: making a durable write depend on the new channel
        differing would leave that run silent and a later one pinning, which is harder to explain
        than always doing it.
        """
        kept: list[PinnedKind] = []
        for borrower, lends in self._fallbacks.items():
            if lends is not object_type:
                continue
            row = await mappings.pin(
                repository_id=repository_id, object_type=borrower, copying=object_type
            )
            if row is not None:
                kept.append(PinnedKind(borrower, row.discord_channel_id))
        return tuple(kept)
