from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from shannon.db.stores.channel_mappings import ChannelMappingStore
from shannon.db.stores.repositories import RepositoryStore
from shannon.domain.enums import ObjectType
from shannon.domain.errors import DuplicateRegistrationError
from shannon.github.client import LooksUpRepository
from shannon.github.urls import parse_repository_url

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RegistrationResult:
    repository_id: int
    full_name: str
    html_url: str
    pr_channel_id: int


class RepositoryRegistrationService:
    """Binds one GitHub repository to one Discord guild."""

    def __init__(self, sessionmaker: async_sessionmaker, github: LooksUpRepository) -> None:
        self._sessionmaker = sessionmaker
        self._github = github

    async def register(self, *, guild_id: int, channel_id: int, link: str) -> RegistrationResult:
        """Register a repository against a guild.

        Raises UnparseableLinkError for a bad link, GitHubNotFoundError when the repository does
        not exist or the token cannot see it, and DuplicateRegistrationError when either side of
        the binding is already taken.
        """
        ref = parse_repository_url(link)
        snapshot = await self._github.get_repository(ref.owner, ref.name)

        async with self._sessionmaker() as session, session.begin():
            repositories = RepositoryStore(session)

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

        logger.info("registered %s to guild %s", result.full_name, guild_id)
        return result
