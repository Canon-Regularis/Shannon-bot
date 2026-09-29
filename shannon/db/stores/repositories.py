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

    async def with_boards(self) -> Sequence[Repository]:
        """Every repository that has a board linked to it, oldest first.

        No `at_most`, unlike `registered` above, because the caller's question is different: that
        one asks whether more than one server exists and is bounded so the answer costs the same
        on a deployment with fifty. This one asks which boards to read, and cutting the list short
        would silently stop mirroring somebody's board with nothing saying so. What bounds it is
        that a board has to be linked by hand.
        """
        return (
            await self._session.scalars(
                select(Repository)
                .where(Repository.project_number.is_not(None))
                .order_by(Repository.id)
            )
        ).all()

    async def linked_to_board(
        self, *, project_number: int, project_owner: str | None
    ) -> Repository | None:
        """A repository already mirroring this exact board, if one is.

        Two repositories sharing a board would each mirror every draft card into their own
        server, because a tracked item is keyed by repository, and nothing else would notice.
        The owner is compared as stored, which means a null owner and an explicit one naming the
        same account read as different boards - accepted, because resolving that here would mean
        a GitHub call inside a uniqueness check.
        """
        found: Repository | None = await self._session.scalar(
            select(Repository).where(
                Repository.project_number == project_number,
                Repository.project_owner == project_owner,
            )
        )
        return found

    async def set_board(
        self, repository: Repository, *, project_number: int | None, project_owner: str | None
    ) -> None:
        """Point a repository at a board, or at none.

        `project_number=None` clears both, because an owner without a number addresses nothing
        and would sit in the row looking like configuration.
        """
        repository.project_number = project_number
        repository.project_owner = project_owner if project_number is not None else None
        await self._session.flush()

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
