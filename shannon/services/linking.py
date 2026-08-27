from __future__ import annotations

import logging
import re

from sqlalchemy.ext.asyncio import async_sessionmaker

from shannon.db.stores.team_links import TeamLinkStore
from shannon.db.stores.user_links import UserLinkStore
from shannon.domain.errors import ShannonError
from shannon.github.client import LooksUpUsers

logger = logging.getLogger(__name__)

# GitHub's own rule, which is narrower than "letters, digits and hyphens": a login may not
# begin or end with a hyphen and may not carry two in a row. Written out because the loose
# version accepted `mona--lisa` and `monalisa-`, names GitHub cannot issue, and a name nothing
# can ever match is the one thing this command must not record.
_GITHUB_LOGIN = re.compile(r"^[A-Za-z0-9](?:-?[A-Za-z0-9]){0,38}$")

# A team slug is more forgiving than a login: GitHub builds it from the display name, so it takes
# underscores and full stops that an account name never would, and it can be longer.
_GITHUB_TEAM = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98})$")


class InvalidGitHubUsernameError(ShannonError):
    """The string given is not shaped like a GitHub login."""


class UserLinkingService:
    """Binds a GitHub login to a Discord account so that person can be pinged."""

    def __init__(self, sessionmaker: async_sessionmaker, github: LooksUpUsers) -> None:
        self._sessionmaker = sessionmaker
        self._github = github

    async def link(self, *, guild_id: int, github_username: str, discord_user_id: int) -> str:
        """Bind a login to an account, refusing one GitHub has never heard of.

        Asked of GitHub rather than only matched against a pattern, because the failure this
        prevents is silent and permanent. A login nobody holds is recorded happily, the command
        answers that it worked, and from then on that person is named in the thread as plain
        text instead of being mentioned, which is exactly what somebody who never linked at all
        looks like. There is nothing in the thread, the block or the log to tell the two apart,
        so neither they nor the server admin has any way to find out.

        One public call, on a command each person runs once. GitHub being unreachable makes this
        fail rather than bind, and the reply says so: a link that cannot be checked is worth
        less than a person trying again in a minute.
        """
        username = github_username.strip().lstrip("@")
        if not _GITHUB_LOGIN.match(username):
            raise InvalidGitHubUsernameError(f"{github_username!r} is not a GitHub username.")
        if not await self._github.user_exists(username):
            raise InvalidGitHubUsernameError(f"GitHub has no user called {username!r}.")

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


class InvalidGitHubTeamError(ShannonError):
    """The string given is not shaped like a GitHub team slug."""


class TeamLinkingService:
    """Binds a GitHub team to a Discord role, so a review asked of it reaches somebody.

    The sibling of the account linking above, and separate because the halves differ all the way
    down: a person claims their own account and anybody may do that for themselves, while
    pointing a role at a team is a decision about the server. That is why the command that drives
    this is gated like `/set_channel` rather than like `/link`.
    """

    def __init__(self, sessionmaker: async_sessionmaker) -> None:
        self._sessionmaker = sessionmaker

    async def link(self, *, guild_id: int, github_team: str, discord_role_id: int) -> str:
        slug = github_team.strip().lstrip("@")
        if not _GITHUB_TEAM.match(slug):
            raise InvalidGitHubTeamError(f"{github_team!r} is not a GitHub team.")

        async with self._sessionmaker() as session, session.begin():
            await TeamLinkStore(session).link(
                guild_id=guild_id, github_team=slug, discord_role_id=discord_role_id
            )

        logger.info("linked team:%s to role:%s in guild %s", slug, discord_role_id, guild_id)
        return slug.lower()
