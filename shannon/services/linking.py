from __future__ import annotations

import logging
import re

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

        async with self._sessionmaker() as session, session.begin():
            await UserLinkStore(session).link(
                guild_id=guild_id,
                github_username=username,
                discord_user_id=discord_user_id,
            )

        logger.info(
            "linked github:%s to discord:%s in guild %s", username, discord_user_id, guild_id
        )
        return username
