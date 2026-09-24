from __future__ import annotations

import logging
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.stores.repositories import RepositoryStore
from shannon.db.stores.team_links import TeamLinkStore
from shannon.db.stores.user_links import UserLinkStore
from shannon.domain.errors import NotRegisteredError, ShannonError
from shannon.domain.text import code_span
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


class NoTeamsHereError(ShannonError):
    """The repository belongs to a person, and a person has no teams."""


class KnowsAccountKinds(Protocol):
    """Whether a GitHub account is an organisation or a person.

    One member, and Metadata-only on GitHub's side: this is the same question the board
    reader asks to decide which path a project lives under, and it goes through the
    ordinary installation rather than needing a credential of its own. Listing an
    organisation's actual teams would not - that wants Members: Read, which the App does
    not hold and which no token narrow enough to be worth adding can cover.
    """

    async def is_organisation(self, owner: str) -> bool: ...


class TeamLinkingService:
    """Binds a GitHub team to a Discord role, so a review asked of it reaches somebody.

    Pointing a role at a team is a decision about the server rather than a claim on one's own
    account, so the command that drives this is gated like `/set_channel` rather than `/link`.

    GitHub is asked one thing and deliberately not another. It is asked what KIND of account
    owns the repository, because a personal account has no teams at all and a mapping made
    against one is dead on arrival with nothing to say so. It is not asked whether the team
    EXISTS: that needs the organisation's membership roster, which the App has no permission
    for and which a second token would only reach by being broad enough to read the
    organisation's people - a worse trade than the typo it would catch. A secret team is
    invisible to such a token anyway, so even that check would refuse mappings that are
    correct.
    """

    def __init__(
        self, sessionmaker: async_sessionmaker[AsyncSession], accounts: KnowsAccountKinds
    ) -> None:
        self._sessionmaker = sessionmaker
        self._accounts = accounts

    async def link(self, *, guild_id: int, github_team: str, discord_role_id: int) -> str:
        slug = github_team.strip().lstrip("@")
        if not is_team_slug(slug):
            raise InvalidGitHubTeamError(f"{code_span(github_team)} is not a GitHub team.")

        async with self._sessionmaker() as session, session.begin():
            repository = await RepositoryStore(session).get_by_guild(guild_id)
            if repository is None:
                raise NotRegisteredError("This server has no repository yet. Run /register first.")

            # A team is an organisation's, and only an organisation's. GitHub has never
            # asked a personal account's repository for a review from a team, so a
            # mapping made on one can never match anything: the role sits there looking
            # configured and is silent for ever. The shape check above passes any slug,
            # which is how this was reachable at all.
            owner = repository.repo_name.partition("/")[0]
            if not await self._accounts.is_organisation(owner):
                raise NoTeamsHereError(
                    f"{owner} is a personal account, and only an organisation has teams. "
                    "GitHub will never ask a team for a review here, so this mapping "
                    "would never match anything."
                )

            await TeamLinkStore(session).link(
                guild_id=guild_id, github_team=slug, discord_role_id=discord_role_id
            )

        logger.info("linked team:%s to role:%s in guild %s", slug, discord_role_id, guild_id)
        return slug.lower()
