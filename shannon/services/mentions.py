"""Whether this bot's messages notify one person in one server.

The polarity flips here and nowhere else: the table records who asked to be left alone, a row
present being the whole of the fact, while the command asks whether mentions are on.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.stores.muted_members import MutedMemberStore

logger = logging.getLogger(__name__)


class MentionPreferences:
    """A member's own answer to whether this bot may notify them here."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def wants_mentions(self, *, guild_id: int, discord_user_id: int) -> bool:
        """Whether this bot may notify them, which is everybody until they say otherwise."""
        async with self._sessionmaker() as session:
            return not await MutedMemberStore(session).is_muted(
                guild_id=guild_id, discord_user_id=discord_user_id
            )

    async def set_mentions(self, *, guild_id: int, discord_user_id: int, wanted: bool) -> None:
        """Record what they asked for, whether or not it is what they already had.

        Both store calls settle a repeat themselves, so clicking twice lands the same as once.
        """
        async with self._sessionmaker() as session, session.begin():
            store = MutedMemberStore(session)
            if wanted:
                await store.unmute(guild_id=guild_id, discord_user_id=discord_user_id)
            else:
                await store.mute(guild_id=guild_id, discord_user_id=discord_user_id)
        logger.info(
            "discord:%s in guild %s %s mentions",
            discord_user_id,
            guild_id,
            "wants" if wanted else "does not want",
        )
