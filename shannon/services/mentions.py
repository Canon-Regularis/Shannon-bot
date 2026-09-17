"""Whether this bot's messages notify one person in one server.

Its own module rather than a third class in `linking.py`. That one binds a GitHub identity to a
Discord one and needs a GitHub client to check the claim; this needs neither, and it is the only
thing in the project a member decides about themselves.

The polarity flips here and nowhere else. The table records who asked to be left alone, because a
row present is the whole of the fact and there is no third state to keep. The command asks whether
mentions are on. Somewhere has to turn one into the other, and doing it in one place means nothing
either side of it has to remember which way round it is.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import async_sessionmaker

from shannon.db.stores.muted_members import MutedMemberStore

logger = logging.getLogger(__name__)


class MentionPreferences:
    """A member's own answer to whether this bot may notify them here."""

    def __init__(self, sessionmaker: async_sessionmaker) -> None:
        self._sessionmaker = sessionmaker

    async def wants_mentions(self, *, guild_id: int, discord_user_id: int) -> bool:
        """Whether this bot may notify them, which is everybody until they say otherwise."""
        async with self._sessionmaker() as session:
            return not await MutedMemberStore(session).is_muted(
                guild_id=guild_id, discord_user_id=discord_user_id
            )

    async def set_mentions(self, *, guild_id: int, discord_user_id: int, wanted: bool) -> None:
        """Record what they asked for, whether or not it is what they already had.

        Both halves settle a repeat themselves rather than reading first, so somebody clicking
        twice is answered the same way as somebody clicking once.
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
