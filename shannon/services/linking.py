from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.stores.team_links import TeamLinkStore
from shannon.db.stores.user_links import UserLinkStore
from shannon.domain.errors import ShannonError
from shannon.github.client import LooksUpUsers
from shannon.github.mentions import is_login, is_team_slug

logger = logging.getLogger(__name__)


class InvalidGitHubUsernameError(ShannonError):
    """The string given is not shaped like a GitHub login."""


class UserLinkingService:
    """Binds a GitHub login to a Discord account so that person can be pinged."""

    def __init__(
        self, sessionmaker: async_sessionmaker[AsyncSession], github: LooksUpUsers
    ) -> None:
        self._sessionmaker = sessionmaker
        self._github = github

    async def link(self, *, guild_id: int, github_username: str, discord_user_id: int) -> str:
        """Bind a login to an account, refusing one GitHub has never heard of.

        Checked against GitHub, not just the pattern: a login nobody holds binds happily and then
        reads as plain text, just like never linking. The numeric account id is stored beside the
        login, because a login can change hands and a mention should follow the person.
        """
        username = github_username.strip().lstrip("@")
        if not is_login(username):
            raise InvalidGitHubUsernameError(f"{github_username!r} is not a GitHub username.")
        github_user_id = await self._github.user_id(username)
        if github_user_id is None:
            raise InvalidGitHubUsernameError(f"GitHub has no user called {username!r}.")

        await self._write(guild_id, username, github_user_id, discord_user_id)

        logger.info(
            "linked github:%s to discord:%s in guild %s", username, discord_user_id, guild_id
        )
        return username

    async def bind(
        self, *, guild_id: int, discord_user_id: int, login: str, github_user_id: int
    ) -> str:
        """Record an account GitHub has just vouched for, against the person it vouched to.

        Nothing is checked and nothing is asked, unlike `link` above, because there is nothing
        left to check: both halves came back from `GET /user` answering about the account that
        had just authorised. Asking GitHub again whether that login exists would be asking it to
        confirm its own sentence.

        It replaces whatever either half was bound to, which `link` already does and which is
        right here for a reason it is not there. A proof beats a claim: if somebody else had
        typed this login against their own Discord account, GitHub has now said whose it is, and
        the row that goes is the one nobody ever vouched for.
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
