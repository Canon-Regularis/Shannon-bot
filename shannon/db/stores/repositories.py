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
        return await self._session.scalar(
            select(Repository).where(Repository.discord_guild_id == discord_guild_id)
        )

    async def get_by_id(self, repository_id: int) -> Repository | None:
        """The repository a row already points at.

        Reading `item.repository` instead would lazy load, which an async session cannot do
        outside its own greenlet and which fails at the point of use rather than here.
        """
        return await self._session.get(Repository, repository_id)

    async def registered(self, *, at_most: int = 2) -> Sequence[Repository]:
        """The registered repositories, oldest first, up to `at_most` of them.

        Replaces `only_one`, which answered "the registered repository, whichever it is" on the
        reasoning that this process serves one guild so asking without a key was asking the only
        question there is. That sentence stopped being true: this bot is invited to a server
        rather than built into one, and its commands register globally so that a second server
        works. Every table that matters is keyed per guild and every other read here takes a key.

        Bounded rather than counted, because the only caller's question is whether there is more
        than one, and a count or an unbounded read would grow with the number of servers on a
        query that runs on every poll.

        Ordered, which is the half of the old reasoning that survived: a deployment answering
        with one of several answers the same way twice rather than alternating.
        """
        return (
            await self._session.scalars(select(Repository).order_by(Repository.id).limit(at_most))
        ).all()

    async def get_by_github_id(self, github_repo_id: int) -> Repository | None:
        return await self._session.scalar(
            select(Repository).where(Repository.github_repo_id == github_repo_id)
        )

    async def add(
        self, *, github_repo_id: int, repo_name: str, repo_url: str, discord_guild_id: int
    ) -> Repository:
        repository = Repository(
            github_repo_id=github_repo_id,
            repo_name=repo_name,
            repo_url=repo_url,
            discord_guild_id=discord_guild_id,
        )
        self._session.add(repository)
        await self._session.flush()
        return repository

    async def follow_rename(self, repository: Repository, *, repo_name: str, repo_url: str) -> bool:
        """Take the name and URL GitHub is using now, reporting whether they moved.

        Webhooks find a repository by its numeric id, which survives a rename, but `/pr` and
        `/issue` compare the link against the stored name. Without this, renaming a repository
        on GitHub leaves the mirror working and both commands answering that the link is for
        the wrong repository, with nothing the server admin can do about it.
        """
        if repository.repo_name == repo_name and repository.repo_url == repo_url:
            return False

        logger.info("%s is now %s, following the rename", repository.repo_name, repo_name)
        repository.repo_name = repo_name
        repository.repo_url = repo_url
        await self._session.flush()
        return True
