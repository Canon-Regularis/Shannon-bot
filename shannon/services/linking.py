from __future__ import annotations

import logging
import re

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from shannon.db.stores.user_links import UserLinkStore
from shannon.domain.errors import ShannonError

logger = logging.getLogger(__name__)

_GITHUB_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")


class InvalidGitHubUsernameError(ShannonError):
    """The string given is not shaped like a GitHub login."""


class UserLinkingService:
    """Binds a GitHub login to a Discord account so that person can be pinged."""

    def __init__(self, sessionmaker: async_sessionmaker) -> None:
        self._sessionmaker = sessionmaker

    async def link(self, *, guild_id: int, github_username: str, discord_user_id: int) -> str:
        username = github_username.strip().lstrip("@")
        if not _GITHUB_LOGIN.match(username):
            raise InvalidGitHubUsernameError(f"{github_username!r} is not a GitHub username.")

        try:
            await self._write(guild_id, username, discord_user_id)
        except IntegrityError:
            # Both halves of a link are unique within a guild and either can be held by a
            # different row, so the store clears both out and writes the pairing fresh. Two of
            # those overlapping have both find nothing to clear and both insert, which is what a
            # double-submitted /link does, and the loser hits the index.
            #
            # Retried rather than reported, because the rollback undid this attempt and left the
            # other one standing, and a second run sees it committed and settles cleanly. Last
            # writer wins, which is what replacing whatever either side had already means.
            logger.info("a concurrent /link beat this one in guild %s, writing again", guild_id)
            await self._write(guild_id, username, discord_user_id)

        logger.info(
            "linked github:%s to discord:%s in guild %s", username, discord_user_id, guild_id
        )
        return username

    async def _write(self, guild_id: int, username: str, discord_user_id: int) -> None:
        async with self._sessionmaker() as session, session.begin():
            await UserLinkStore(session).link(
                guild_id=guild_id,
                github_username=username,
                discord_user_id=discord_user_id,
            )
