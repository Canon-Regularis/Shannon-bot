from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from shannon.db.stores.channel_mappings import ChannelMappingStore
from shannon.db.stores.installations import InstallationStore
from shannon.db.stores.repositories import RepositoryStore
from shannon.domain.enums import ObjectType
from shannon.domain.errors import DuplicateRegistrationError, NotInstalledError
from shannon.github.client import LooksUpRepository
from shannon.github.urls import parse_repository_url

# What somebody is told to do about a repository this bot cannot see. The slug is read from
# GitHub rather than configured, so this is built rather than written down whole.
INSTALL_URL = "https://github.com/apps/{slug}/installations/new"

logger = logging.getLogger(__name__)


class FindsInstallations(Protocol):
    """Asking GitHub whether the App is installed on a repository, and what it is called.

    Its own protocol rather than the whole token minter, because this is all registration needs:
    it never mints a token itself, it only wants to know whether one could be minted.
    """

    async def installed_on(self, owner: str, name: str) -> int | None: ...

    async def app_slug(self) -> str: ...


@dataclass(frozen=True, slots=True)
class RegistrationResult:
    repository_id: int
    full_name: str
    html_url: str
    pr_channel_id: int


class RepositoryRegistrationService:
    """Binds one GitHub repository to one Discord guild."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        github: LooksUpRepository,
        installations: FindsInstallations | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._github = github
        # Optional so that every test which built this before the App existed still builds one.
        # Left out, the installation check is skipped and the behaviour is exactly what it was:
        # read the repository and let a 404 speak for itself.
        self._installations = installations

    async def register(self, *, guild_id: int, channel_id: int, link: str) -> RegistrationResult:
        """Register a repository against a guild.

        Raises UnparseableLinkError for a bad link, NotInstalledError when the GitHub App is not
        installed on the repository, GitHubNotFoundError when it is installed and the repository
        still cannot be read, and DuplicateRegistrationError when either side of the binding is
        already taken.

        The installation is checked BEFORE the repository is read, and that order is the fix for
        issue #98 rather than an optimisation. Read first, a private repository answers 404 and
        the person is told it could not be found, so they go and check a link that was correct.
        Asked first, the answer is that the App is not installed, which is both true and
        actionable.
        """
        ref = parse_repository_url(link)
        installation = await self._installation_for(ref.owner, ref.name)
        snapshot = await self._github.get_repository(ref.owner, ref.name)

        async with self._sessionmaker() as session, session.begin():
            repositories = RepositoryStore(session)
            if installation is not None:
                # Written down on the way past, so the next call about this owner resolves from
                # the database rather than asking GitHub again. `/register` is the one command
                # that always knows the answer, so it is the cheapest place to learn it.
                await InstallationStore(session).remember(
                    installation_id=installation, account_login=ref.owner
                )

            existing = await repositories.get_by_guild(guild_id)
            if existing is not None:
                raise DuplicateRegistrationError(
                    f"This server is already registered to {existing.repo_name}"
                )

            elsewhere = await repositories.get_by_github_id(snapshot.github_repo_id)
            if elsewhere is not None:
                raise DuplicateRegistrationError(
                    f"{snapshot.full_name} is already registered to another server"
                )

            try:
                repository = await repositories.add(
                    github_repo_id=snapshot.github_repo_id,
                    repo_name=snapshot.full_name,
                    repo_url=snapshot.html_url,
                    discord_guild_id=guild_id,
                    private=snapshot.private,
                )
            except IntegrityError as conflict:
                # Two people running /register at the same moment both get past the checks
                # above. The database settles it, and the loser should hear the same thing it
                # would have heard a second later.
                raise DuplicateRegistrationError(
                    "This server was registered a moment ago. Try /register again to see where."
                ) from conflict
            # The channel the command was run in becomes the home for PR threads.
            await ChannelMappingStore(session).set(
                repository_id=repository.id,
                object_type=ObjectType.PR,
                discord_channel_id=channel_id,
            )
            result = RegistrationResult(
                repository_id=repository.id,
                full_name=snapshot.full_name,
                html_url=snapshot.html_url,
                pr_channel_id=channel_id,
            )

        logger.info(
            "registered %s to guild %s%s",
            result.full_name,
            guild_id,
            " (private)" if snapshot.private else "",
        )
        return result

    async def _installation_for(self, owner: str, name: str) -> int | None:
        """Which installation covers this repository, refusing if none does.

        Skipped entirely when nothing was wired in, which is how every caller written before the
        App existed goes on working unchanged.
        """
        if self._installations is None:
            return None

        installation = await self._installations.installed_on(owner, name)
        if installation is not None:
            return installation

        slug = await self._installations.app_slug()
        where = f" {INSTALL_URL.format(slug=slug)}" if slug else ""
        raise NotInstalledError(
            f"This bot cannot see {owner}/{name}. Install the GitHub App on it and run "
            f"/register again.{where}"
        )
