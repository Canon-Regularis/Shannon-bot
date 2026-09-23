from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.stores.channel_mappings import ChannelMappingStore
from shannon.db.stores.installations import InstallationStore
from shannon.db.stores.repositories import RepositoryStore
from shannon.domain.enums import ObjectType
from shannon.domain.errors import (
    DuplicateRegistrationError,
    NotInstalledError,
    NotProvenError,
)
from shannon.domain.models import RepositorySnapshot
from shannon.github.urls import parse_repository_url

# Where somebody is sent to install the App. The slug is read from GitHub rather than
# configured, so the URL is built rather than written down whole.
INSTALL_URL = "https://github.com/apps/{slug}/installations/new"

logger = logging.getLogger(__name__)

# The only permission that may bind a repository, and the same bar `/unregister` sets to unbind
# one. Symmetric on purpose: mirroring a repository into a Discord channel is a disclosure, and
# whoever owns the repository is the one entitled to make it. GitHub folds `maintain` into
# `write` and `triage` into `read` before answering, so this is the whole of the admin tier.
ADMIN = "admin"


class ReadsRepositories(Protocol):
    """Reading a repository, and what one account may do to it.

    Two questions and no more, so the service that can bind a repository holds nothing that could
    write to one. The same shape as `unregistration.ReadsPermissions`, widened by the one call
    this path also needs.
    """

    async def get_repository(self, owner: str, name: str) -> RepositorySnapshot: ...

    async def permission_for(self, owner: str, name: str, login: str) -> str: ...


class FindsInstallations(Protocol):
    """Asking GitHub whether the App is installed on a repository, and what it is called."""

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
        sessionmaker: async_sessionmaker[AsyncSession],
        github: ReadsRepositories,
        installations: FindsInstallations | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._github = github
        # Left out, the installation check is skipped and a 404 from reading the repository
        # speaks for itself.
        self._installations = installations

    async def register(
        self, *, guild_id: int, channel_id: int, link: str, login: str
    ) -> RegistrationResult:
        """Register a repository against a guild, for somebody GitHub says administers it.

        Raises UnparseableLinkError for a bad link, NotInstalledError when the GitHub App is not
        installed, NotProvenError when `login` does not administer the repository,
        GitHubNotFoundError when it is installed and the repository still cannot be read, and
        DuplicateRegistrationError when either side of the binding is taken. The installation is
        checked before the repository is read: read first, a private repository answers 404 and
        the person goes and checks a link that was perfectly correct.

        `login` must be one GitHub vouched for a moment ago rather than one out of `user_links`,
        for the reason `/unregister` gives: anybody with the Admin role in the server can put
        what they like in there. Issue #135.
        """
        ref = parse_repository_url(link)
        installation = await self._installation_for(ref.owner, ref.name)
        await self._remember(installation, ref.owner)
        await self._refuse_anybody_who_does_not_administer_it(ref.owner, ref.name, login)
        snapshot = await self._github.get_repository(ref.owner, ref.name)

        async with self._sessionmaker() as session, session.begin():
            repositories = RepositoryStore(session)
            existing = await repositories.get_by_guild(guild_id)
            if existing is not None:
                raise DuplicateRegistrationError(
                    f"This server is already registered to {existing.repo_name}."
                )

            elsewhere = await repositories.get_by_github_id(snapshot.github_repo_id)
            if elsewhere is not None:
                raise DuplicateRegistrationError(
                    f"{snapshot.full_name} is already registered to another server."
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
                # above, so the database settles it and the loser hears the same refusal.
                raise DuplicateRegistrationError(
                    "This server was registered a moment ago. Try /register again to see where."
                ) from conflict
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

    async def _remember(self, installation: int | None, owner: str) -> None:
        """Write the installation down before anything asks GitHub a question as this bot.

        Before, and in its own transaction, because every token-bearing call below resolves its
        token through this row. It used to be written on the way past inside the transaction that
        does the binding, which is after the repository has already been read: on an owner whose
        row no webhook ever wrote, the first `/register` read the repository anonymously, and a
        private one answered 404 and was reported as a repository that does not exist.

        The permission check added for #135 makes that worse rather than better — an anonymous
        collaborators call is a guaranteed 404, which reads as `none`, so every first registration
        for a fresh owner would refuse somebody who does administer it.
        """
        if installation is None:
            return
        async with self._sessionmaker() as session, session.begin():
            await InstallationStore(session).remember(
                installation_id=installation, account_login=owner
            )

    async def _refuse_anybody_who_does_not_administer_it(
        self, owner: str, name: str, login: str
    ) -> None:
        """Whether GitHub says this account administers the repository about to be bound.

        Asked of GitHub every time rather than recorded, which is what makes the answer worth
        anything: a proof says who somebody is, and what they may do is re-derived. `permission_for`
        answers `none` to a 404, so an account GitHub has never heard of fails closed.
        """
        permission = await self._github.permission_for(owner, name, login)
        if permission == ADMIN:
            return
        logger.info(
            "refusing to register %s/%s for github:%s, who has %r", owner, name, login, permission
        )
        raise NotProvenError(
            f"You are signed in as {login}, who does not have admin on {owner}/{name}. Only "
            "somebody who can administer the repository can mirror it into a Discord server."
        )

    async def _installation_for(self, owner: str, name: str) -> int | None:
        """Which installation covers this repository, refusing if none does."""
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
