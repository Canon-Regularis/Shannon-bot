from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shannon.db.models import Repository


class RepositoryStore:
    """Data access for registered GitHub repositories."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_guild(self, discord_guild_id: int) -> Repository | None:
        return await self._session.scalar(
            select(Repository).where(Repository.discord_guild_id == discord_guild_id)
        )

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
