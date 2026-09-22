from __future__ import annotations

import logging
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shannon.db.models import Repository

logger = logging.getLogger(__name__)


class RepositoryStore:
    """Data access for registered GitHub repositories."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_guild(self, discord_guild_id: int) -> Repository | None:
        found: Repository | None = await self._session.scalar(
            select(Repository).where(Repository.discord_guild_id == discord_guild_id)
        )
        return found

    async def get_by_id(self, repository_id: int) -> Repository | None:
        """The repository a row already points at.

        Reading `item.repository` instead would lazy load, which an async session cannot do
        outside its own greenlet and which fails at the point of use rather than here.
        """
        return await self._session.get(Repository, repository_id)

    async def registered(self, *, at_most: int = 2) -> Sequence[Repository]:
        """The registered repositories, oldest first, up to `at_most` of them.

        The one read here that takes no guild key: the caller's question is whether more than one
        server has registered. Bounded rather than counted, because a count would grow with the
        number of servers on a query that runs on every poll. Ordered so that a deployment with
        several of them answers the same way twice rather than alternating.
        """
        return (
            await self._session.scalars(select(Repository).order_by(Repository.id).limit(at_most))
        ).all()

    async def get_by_github_id(self, github_repo_id: int) -> Repository | None:
        found: Repository | None = await self._session.scalar(
            select(Repository).where(Repository.github_repo_id == github_repo_id)
        )
        return found

    async def add(
        self,
        *,
        github_repo_id: int,
        repo_name: str,
        repo_url: str,
        discord_guild_id: int,
        private: bool | None = None,
    ) -> Repository:
        repository = Repository(
            github_repo_id=github_repo_id,
            repo_name=repo_name,
            repo_url=repo_url,
            discord_guild_id=discord_guild_id,
            private=private,
        )
        self._session.add(repository)
        await self._session.flush()
        return repository

    async def follow_rename(
        self,
        repository: Repository,
        *,
        repo_name: str,
        repo_url: str,
        private: bool | None = None,
    ) -> bool:
        """Take the name, URL and visibility GitHub is using now, reporting whether the NAME moved.

        Webhooks find a repository by its numeric id, which survives a rename, but `/pr` and
        `/issue` compare the link against the stored name, so without this both answer that the
        link is for the wrong repository. Visibility rides along on the same object, written only
        where GitHub said, which fills in a row stored before the column existed. It does not
        affect the answer: a repository quietly flipped to private has not been renamed.
        """
        moved = repository.repo_name != repo_name or repository.repo_url != repo_url
        revealed = private is not None and repository.private != private
        if revealed:
            logger.info("%s is now %s", repository.repo_name, "private" if private else "public")
            repository.private = private

        if not moved:
            # A delivery saying nothing new must leave the row completely alone, or `updated_at`
            # moves on every unrelated event and stops meaning anything. Flushed where the
            # visibility did change, so that write does not wait for whatever commits next.
            if revealed:
                await self._session.flush()
            return False

        logger.info("%s is now %s, following the rename", repository.repo_name, repo_name)
        repository.repo_name = repo_name
        repository.repo_url = repo_url
        await self._session.flush()
        return True
