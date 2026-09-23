from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.stores.team_links import TeamLinkStore
from shannon.db.stores.user_links import UserLinkStore
from shannon.domain.errors import ShannonError
from shannon.github.mentions import is_team_slug

logger = logging.getLogger(__name__)


class UserLinkingService:
    """Records the GitHub account GitHub itself vouched for, against the Discord one it told.

    It used to take a login somebody typed and check only that it existed, which is the hole
    issue #144 closed: a login that exists says nothing about whose it is. Nothing types one any
    more, so there is nothing left here to validate and nothing to ask GitHub, and what the class
    does is write down an answer that has already been given.
    """

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def bind(
        self, *, guild_id: int, discord_user_id: int, login: str, github_user_id: int
    ) -> str:
        """Record an account GitHub has just vouched for, against the person it vouched to.

        Nothing is checked and nothing is asked, because there is nothing left to check: both
        halves came back from `GET /user` answering about the account that had just authorised.
        Asking GitHub again whether that login exists would be asking it to confirm its own
        sentence.

        It replaces whatever either half was bound to. A proof beats a claim: if somebody else
        had typed this login against their own Discord account back when typing one was how this
        worked, GitHub has now said whose it is, and the row that goes is the one nobody ever
        vouched for.
        """
        username = login.strip().lstrip("@").lower()
        await self._write(guild_id, username, github_user_id, discord_user_id)

        logger.info(
            "github:%s proved to discord:%s in guild %s, and is now linked there",
            username,
            discord_user_id,
            guild_id,
        )
        return username

    async def _write(
        self, guild_id: int, username: str, github_user_id: int, discord_user_id: int
    ) -> None:
        async with self._sessionmaker() as session, session.begin():
            await UserLinkStore(session).link(
                guild_id=guild_id,
                github_username=username,
                github_user_id=github_user_id,
                discord_user_id=discord_user_id,
            )


class InvalidGitHubTeamError(ShannonError):
    """The string given is not shaped like a GitHub team slug."""


class TeamLinkingService:
    """Binds a GitHub team to a Discord role, so a review asked of it reaches somebody.

    Pointing a role at a team is a decision about the server rather than a claim on one's own
    account, so the command that drives this is gated like `/set_channel` rather than `/link`.
    """

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def link(self, *, guild_id: int, github_team: str, discord_role_id: int) -> str:
        slug = github_team.strip().lstrip("@")
        if not is_team_slug(slug):
            raise InvalidGitHubTeamError(f"{github_team!r} is not a GitHub team.")

        async with self._sessionmaker() as session, session.begin():
            await TeamLinkStore(session).link(
                guild_id=guild_id, github_team=slug, discord_role_id=discord_role_id
            )

        logger.info("linked team:%s to role:%s in guild %s", slug, discord_role_id, guild_id)
        return slug.lower()
